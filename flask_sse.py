# coding=utf-8
from __future__ import unicode_literals

from collections import OrderedDict
from flask import Blueprint, request, current_app, json, stream_with_context
from redis import StrictRedis
from redis.exceptions import ConnectionError
import six

__version__ = '0.2.1'


@six.python_2_unicode_compatible
class Message(object):
    """
    Data that is published as a server-sent event.
    """
    def __init__(self, data, type=None, id=None, retry=None):
        """
        Create a server-sent event.

        :param data: The event data. If it is not a string, it will be
            serialized to JSON using the Flask application's
            :class:`~flask.json.JSONEncoder`.
        :param type: An optional event type.
        :param id: An optional event ID.
        :param retry: An optional integer, to specify the reconnect time for
            disconnected clients of this stream.
        """
        self.data = data
        self.type = type
        self.id = id
        self.retry = retry

    def to_dict(self):
        """
        Serialize this object to a minimal dictionary, for storing in Redis.
        """
        # data is required, all others are optional
        d = {"data": self.data}
        if self.type:
            d["type"] = self.type
        if self.id:
            d["id"] = self.id
        if self.retry:
            d["retry"] = self.retry
        return d

    def block(self):
        """
        Serialize this object as a server-sent block of lines
        """
        return str(self)

    def __str__(self):
        """
        Serialize this object to a string, according to the `server-sent events
        specification <https://www.w3.org/TR/eventsource/>`_.
        """
        if isinstance(self.data, six.string_types):
            data = self.data
        else:
            data = json.dumps(self.data)
        lines = ["data:{value}".format(value=line) for line in data.splitlines()]
        if self.type:
            lines.insert(0, "event:{value}".format(value=self.type))
        if self.id:
            lines.append("id:{value}".format(value=self.id))
        if self.retry:
            lines.append("retry:{value}".format(value=self.retry))
        return "\n".join(lines) + "\n\n"

    def __repr__(self):
        kwargs = OrderedDict()
        if self.type:
            kwargs["type"] = self.type
        if self.id:
            kwargs["id"] = self.id
        if self.retry:
            kwargs["retry"] = self.retry
        kwargs_repr = "".join(
            ", {key}={value!r}".format(key=key, value=value)
            for key, value in kwargs.items()
        )
        return "{classname}({data!r}{kwargs})".format(
            classname=self.__class__.__name__,
            data=self.data,
            kwargs=kwargs_repr,
        )

    def __eq__(self, other):
        return (
            isinstance(other, self.__class__) and
            self.data == other.data and
            self.type == other.type and
            self.id == other.id and
            self.retry == other.retry
        )


class ServerSentEventsBaseBlueprint(Blueprint):
    """
    A :class:`flask.Blueprint` subclass that knows how to publish, subscribe to,
    and stream server-sent events.
    """
    @property
    def redis(self):
        """
        A :class:`redis.StrictRedis` instance, configured to connect to the
        current application's Redis server.
        """
        redis_url = current_app.config.get("SSE_REDIS_URL")
        if not redis_url:
            redis_url = current_app.config.get("REDIS_URL")
        if not redis_url:
            raise KeyError("Must set a redis connection URL in app config.")
        return StrictRedis.from_url(redis_url)

    def publish(self, data, type=None, id=None, retry=None, channel='sse'):
        """
        Publish data as a server-sent event.

        :param data: The event data. If it is not a string, it will be
            serialized to JSON using the Flask application's
            :class:`~flask.json.JSONEncoder`.
        :param type: An optional event type.
        :param id: An optional event ID.
        :param retry: An optional integer, to specify the reconnect time for
            disconnected clients of this stream.
        :param channel: If you want to direct different events to different
            clients, you may specify a channel for this event to go to.
            Only clients listening to the same channel will receive this event.
            Defaults to "sse".
        """
        message = Message(data, type=type, id=id, retry=retry)
        msg_json = json.dumps(message.to_dict())
        return self.redis.publish(channel=channel, message=msg_json)

    def messages(self, channel='sse'):
        """
        A generator of :class:`~flask_sse.Message` objects from the given channel.
        """
        pubsub = self.redis.pubsub()
        pubsub.subscribe(channel)
        try:
            for pubsub_message in pubsub.listen():
                if pubsub_message['type'] == 'message':
                    msg_dict = json.loads(pubsub_message['data'])
                    yield Message(**msg_dict)
        finally:
            try:
                pubsub.unsubscribe(channel)
            except ConnectionError:
                pass

    def stream(self):
        """
        A view function that streams server-sent events. Ignores any
        :mailheader:`Last-Event-ID` headers in the HTTP request.
        Use a "channel" query parameter to stream events from a different
        channel than the default channel (which is "sse").
        """
        channel = request.args.get('channel') or 'sse'

        @stream_with_context
        def generator():
            for message in self.messages(channel=channel):
                yield str(message)

        return current_app.response_class(
            generator(),
            mimetype='text/event-stream',
        )


ssebase = ServerSentEventsBaseBlueprint('ssebase', __name__)
"""
An instance of :class:`~flask_sse.ServerSentEventsBaseBlueprint`
that hooks up the :meth:`~flask_sse.ServerSentEventsBaseBlueprint.stream`
method as a view function at the root of the blueprint. If you don't
want to customize this blueprint at all, you can simply import and
use this instance in your application.
"""
ssebase.add_url_rule(rule="", endpoint="basestream", view_func=ssebase.stream)


class ServerSentEventsBlueprint(ServerSentEventsBaseBlueprint):
    """
    A :class:`flask_sse.ServerSentEventsBaseBlueprint` subclass that provides mechanisms to control
    server-sent events stream and connection.
    """
    def done_streaming(self, channel='sse'):
        """
        Done streaming on this channel if a redis key does not exist.
        This functionality is enabled with SSE_REDIS_CHANNEL_KEY_PREFIX configuration.
        """
        prefix = current_app.config.get("SSE_REDIS_CHANNEL_KEY_PREFIX")
        if prefix is None:
            return None  # never done streaming

        key = prefix + channel
        return not self.redis.exists(key)  # not done streaming if key exists

    def control(self, command, channel='sse'):
        """
        Control the message generator :meth:`messages` by publishing a message.
        (This is not sent to client).

        :param command: E.g. 'disconnect'
        :param channel: If you want to direct different events to different
            clients, you may specify a channel for this event to go to.
            Only clients listening to the same channel will receive this event.
            Defaults to "sse".
        """
        msg_dict = {'sse-control': command}
        msg_json = json.dumps(msg_dict)
        return self.redis.publish(channel=channel, message=msg_json)

    def blocks(self, channel='sse'):
        """
        A generator of streaming blocks of lines from the given channel
          Yields serialized :class:`~flask_sse.Message` objects published to the given channel.
            See :meth:`publish`. This will fire an SSEvent.
          Yields a health-check on timeout. This functionality is enabled with SSE_HEALTH_CHECK
            configuration and is based on SSE_TIMEOUT configuration.
          Yields a health-check when a "health-check" control message is received.
            See :meth:`control`.
          Note: a health-check does not fire an SSEvent.
          Stops when :meth:`done_streaming` returns True. This check is performed periodically
            based on SSE_TIMEOUT configuration.
          Stops when a "disconnect" control message is received. See :meth:`control`.
        """
        health_check = current_app.config.get("SSE_HEALTH_CHECK")
        if health_check is not None:
            # If the line starts with a U+003A COLON character (:), it will be ignored according
            # to the `server-sent events specification <https://www.w3.org/TR/eventsource/>`_.
            health_check = ":{}\n".format(health_check)

        timeout = current_app.config.get("SSE_TIMEOUT")
        if timeout is None and (health_check or self.done_streaming(channel) is not None):
            timeout = 15.0  # timeout is needed to do a health-check or check if done streaming

        pubsub = self.redis.pubsub()
        pubsub.subscribe(channel)
        try:
            while not self.done_streaming(channel):
                pubsub_message = pubsub.get_message(timeout=timeout)
                if pubsub_message is None:
                    if health_check:
                        yield health_check
                elif pubsub_message['type'] == 'message':  # ignore 'subscribe' 'unsubscribe'
                    msg_dict = json.loads(pubsub_message['data'])
                    command = msg_dict.get('sse-control')
                    if command is None:
                        yield Message(**msg_dict).block()
                    elif command == 'health-check':
                        if health_check:
                            yield health_check
                    elif command == 'disconnect':
                        break
        finally:
            try:
                pubsub.unsubscribe(channel)
            except ConnectionError:
                pass

    def stream(self):
        """
        A view function that streams server-sent events. Ignores any
        :mailheader:`Last-Event-ID` headers in the HTTP request.
        Use a "channel" query parameter to stream events from a different
        channel than the default channel (which is "sse").
        """
        channel = request.args.get('channel') or 'sse'

        if self.done_streaming(channel):
            # A client can be told to stop reconnecting using the HTTP 204 No Content
            # response code, according to the `server-sent events specification
            # <https://www.w3.org/TR/eventsource/>`_.
            return current_app.response_class("", status=204)

        @stream_with_context
        def generator():
            for block in self.blocks(channel=channel):
                yield block

        return current_app.response_class(
            generator(),
            mimetype='text/event-stream',
        )


sse = ServerSentEventsBlueprint('sse', __name__)
"""
An instance of :class:`~flask_sse.ServerSentEventsBlueprint`
that hooks up the :meth:`~flask_sse.ServerSentEventsBlueprint.stream`
method as a view function at the root of the blueprint. If you don't
want to customize this blueprint at all, you can simply import and
use this instance in your application.
"""
sse.add_url_rule(rule="", endpoint="stream", view_func=sse.stream)
