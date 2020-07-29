import pytest
import flask_sse
import redis
import types
import flask

pytestmark = pytest.mark.usefixtures("appctx")


@pytest.fixture
def bp(app):
    _bp = flask_sse.ServerSentEventsBlueprint('test-sse', __name__)
    app.register_blueprint(_bp, url_prefix='/stream')
    return _bp


def mock_finite_stream(mocker, N=1):
    # Under normal conditions, generator is never done streaming. For testing purposes, this patch
    # stops the generator after yielding N messages. First 2 calls are made before looping begins.
    # 1st call is for deciding whether to start generator. 2nd call decides timeout value.
    # A message is yielded after the 3rd call. The last call terminates the loop.
    mocker.patch("flask_sse.ServerSentEventsBlueprint.done_streaming",
                 side_effect=[None, None] + [None] * N + [True])


def test_no_redis_configured(bp):
    with pytest.raises(KeyError) as excinfo:
        bp.redis

    expected = 'Must set a redis connection URL in app config.'
    assert str(excinfo.value.args[0]) == expected


def test_redis_url_config(bp, app):
    app.config["REDIS_URL"] = "redis://localhost"
    assert isinstance(bp.redis, redis.StrictRedis)
    assert bp.redis.connection_pool.connection_kwargs['host'] == 'localhost'


def test_sse_redis_url_config(bp, app):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    assert isinstance(bp.redis, redis.StrictRedis)
    assert bp.redis.connection_pool.connection_kwargs['host'] == 'localhost'


def test_config_priority(bp, app):
    app.config["REDIS_URL"] = "redis://1.1.1.1"
    app.config["SSE_REDIS_URL"] = "redis://2.2.2.2"
    assert isinstance(bp.redis, redis.StrictRedis)
    assert bp.redis.connection_pool.connection_kwargs['host'] == '2.2.2.2'


def test_publish_nothing(bp, app):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    with pytest.raises(TypeError):
        bp.publish()


def test_publish(bp, app, mockredis):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    bp.publish("thing")
    mockredis.publish.assert_called_with(channel='sse', message='{"data": "thing"}')


def test_publish_channel(bp, app, mockredis):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    bp.publish("thing", channel='garden')
    mockredis.publish.assert_called_with(channel='garden', message='{"data": "thing"}')


def test_publish_type(bp, app, mockredis):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    bp.publish("thing", type='example')
    mockredis.publish.assert_called_with(
        channel='sse',
        message='{"data": "thing", "type": "example"}',
    )


def test_control(bp, app, mockredis):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    bp.control("command")
    mockredis.publish.assert_called_with(channel='sse', message='{"sse-control": "command"}')


def test_messages(bp, app, mockredis):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    pubsub = mockredis.pubsub.return_value
    pubsub.get_message.side_effect = [
        {
            "type": "message",
            "data": '{"data": "thing", "type": "example"}',
        }
    ]

    gen = bp.messages()

    assert isinstance(gen, types.GeneratorType)
    output = next(gen)
    assert output == flask_sse.Message("thing", type="example").to_str()
    pubsub.subscribe.assert_called_with('sse')


def test_messages_channel(bp, app, mockredis):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    pubsub = mockredis.pubsub.return_value
    pubsub.get_message.side_effect = [
        {
            "type": "message",
            "data": '{"data": "whee", "id": "abc"}',
        }
    ]

    gen = bp.messages('whee')

    assert isinstance(gen, types.GeneratorType)
    output = next(gen)
    assert output == flask_sse.Message("whee", id="abc").to_str()
    pubsub.subscribe.assert_called_with('whee')


def test_messages_close(bp, app, mockredis):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    pubsub = mockredis.pubsub.return_value
    pubsub.get_message.side_effect = [
        {
            "type": "message",
            "data": '{"data": "whee", "id": "abc"}',
        }
    ]

    gen = bp.messages('whee')

    output = next(gen)
    assert output == flask_sse.Message("whee", id="abc").to_str()
    pubsub.subscribe.assert_called_with('whee')
    pubsub.unsubscribe.assert_not_called()
    gen.close()
    pubsub.unsubscribe.assert_called_with('whee')


def test_messages_redis_connection_error(bp, app, mockredis):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    pubsub = mockredis.pubsub.return_value
    pubsub.get_message.side_effect = Exception("Redis uncaught1")
    pubsub.unsubscribe.side_effect = [
        Exception("Redis uncaught2"),
        redis.exceptions.ConnectionError()
    ]

    gen = bp.messages('whee')
    with pytest.raises(Exception, match="Redis uncaught[12]"):
        next(gen)

    gen = bp.messages('whee')
    with pytest.raises(Exception, match="Redis uncaught1"):
        next(gen)


def test_messages_control_not_supported(bp, app, mockredis):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    pubsub = mockredis.pubsub.return_value
    pubsub.get_message.side_effect = [
        {
            "type": "message",
            "data": '{"sse-control": "not-supported"}',
        },
        {
            "type": "message",
            "data": '{"data": "whee", "id": "abc"}',
        }
    ]

    gen = bp.messages('whee')
    output = next(gen)
    assert output == flask_sse.Message("whee", id="abc").to_str()


def test_messages_control_health_check(bp, app, mockredis):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    app.config["SSE_HEALTH_CHECK"] = "HC"
    pubsub = mockredis.pubsub.return_value
    pubsub.get_message.side_effect = [
        {
            "type": "message",
            "data": '{"sse-control": "health-check"}',
        }
    ]

    gen = bp.messages('whee')

    output = next(gen)
    assert output == ':HC\n'


def test_messages_control_disconnect(bp, app, mockredis):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    pubsub = mockredis.pubsub.return_value
    pubsub.get_message.side_effect = [
        {
            "type": "message",
            "data": '{"sse-control": "disconnect"}',
        }
    ]

    gen = bp.messages('whee')

    with pytest.raises(StopIteration):
        output = next(gen)
    pubsub.unsubscribe.assert_called_with('whee')


def test_done_streaming_config(bp, app, mockredis):
    mockredis.exists.return_value = 1
    assert bp.done_streaming() is None
    mockredis.exists.assert_not_called()

    app.config["SSE_REDIS_URL"] = "redis://localhost"
    prefix = "prefix:"
    app.config["SSE_REDIS_CHANNEL_KEY_PREFIX"] = prefix

    mockredis.exists.return_value = 0
    assert bp.done_streaming() is True
    mockredis.exists.assert_called_with(prefix + 'sse')

    mockredis.exists.return_value = 1
    assert bp.done_streaming() is False
    mockredis.exists.assert_called_with(prefix + 'sse')


def test_messages_done_streaming(bp, app, mockredis):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    app.config["SSE_REDIS_CHANNEL_KEY_PREFIX"] = ""
    pubsub = mockredis.pubsub.return_value
    pubsub.get_message.side_effect = [
        {
            "type": "message",
            "data": '{"data": "whee", "id": "1"}',
        },
        {
            "type": "message",
            "data": '{"data": "whee", "id": "2"}',
        }
    ]

    gen = bp.messages('whee')

    mockredis.exists.return_value = 1
    output = next(gen)
    assert output == flask_sse.Message("whee", id="1").to_str()
    pubsub.unsubscribe.assert_not_called()
    assert pubsub.get_message.call_count == 1

    mockredis.exists.return_value = 0
    with pytest.raises(StopIteration):
        next(gen)
    pubsub.unsubscribe.assert_called_with('whee')
    assert pubsub.get_message.call_count == 1


def test_messages_timeout_default_config(bp, app, mockredis):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    pubsub = mockredis.pubsub.return_value
    pubsub.get_message.side_effect = [
        None,
        {
            "type": "message",
            "data": '{"data": "whee", "id": "abc"}',
        }
    ]

    gen = bp.messages('whee')

    output = next(gen)
    assert output == flask_sse.Message("whee", id="abc").to_str()
    pubsub.get_message.assert_called_with(timeout=None)


def test_messages_timeout_done_streaming_never(bp, app, mockredis):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    pubsub = mockredis.pubsub.return_value
    pubsub.get_message.side_effect = [
        None,
        {
            "type": "message",
            "data": '{"data": "whee", "id": "abc"}',
        }
    ]

    gen = bp.messages('whee')

    output = next(gen)
    assert output == flask_sse.Message("whee", id="abc").to_str()
    pubsub.get_message.assert_called_with(timeout=None)
    pubsub.unsubscribe.assert_not_called()


def test_messages_timeout_done_streaming_not(bp, app, mockredis):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    app.config["SSE_REDIS_CHANNEL_KEY_PREFIX"] = ""
    mockredis.exists.return_value = 1
    pubsub = mockredis.pubsub.return_value
    pubsub.get_message.side_effect = [
        None,
        {
            "type": "message",
            "data": '{"data": "whee", "id": "abc"}',
        }
    ]
    gen = bp.messages('whee')

    output = next(gen)
    assert output == flask_sse.Message("whee", id="abc").to_str()
    pubsub.get_message.assert_called_with(timeout=15.0)
    pubsub.unsubscribe.assert_not_called()


def test_messages_timeout_health_check(bp, app, mockredis):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    app.config["SSE_HEALTH_CHECK"] = ""
    pubsub = mockredis.pubsub.return_value
    pubsub.get_message.side_effect = [
        None,
        {
            "type": "message",
            "data": '{"data": "whee", "id": "abc"}',
        }
    ]

    gen = bp.messages('whee')

    output = next(gen)
    assert output == ':\n'
    pubsub.get_message.assert_called_with(timeout=15.0)


def test_stream_done_streaming(bp, app, mockredis):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    app.config["SSE_REDIS_CHANNEL_KEY_PREFIX"] = ""
    mockredis.exists.return_value = 0
    pubsub = mockredis.pubsub.return_value
    pubsub.get_message.side_effect = [
        {
            "type": "message",
            "data": '{"data": "thing", "type": "example"}',
        }
    ]

    resp = bp.stream()

    assert isinstance(resp, flask.Response)
    assert not resp.is_streamed
    assert resp.status_code == 204
    assert resp.content_length == 0
    output = resp.get_data(as_text=True)
    assert output == ""
    pubsub.subscribe.assert_not_called()


def test_stream_disconnect(bp, app, mockredis):
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    pubsub = mockredis.pubsub.return_value
    pubsub.get_message.side_effect = [
        {
            "type": "message",
            "data": '{"sse-control": "disconnect"}',
        }
    ]

    resp = bp.stream()

    assert isinstance(resp, flask.Response)
    assert resp.mimetype == "text/event-stream"
    assert resp.is_streamed
    assert resp.status_code == 200
    output = resp.get_data(as_text=True)
    assert output == ""
    pubsub.subscribe.assert_called_with('sse')
    pubsub.unsubscribe.assert_called_with('sse')


def test_stream(bp, app, mockredis, mocker):
    mock_finite_stream(mocker)
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    pubsub = mockredis.pubsub.return_value
    pubsub.get_message.side_effect = [
        {
            "type": "message",
            "data": '{"data": "thing", "type": "example"}',
        }
    ]

    resp = bp.stream()

    assert isinstance(resp, flask.Response)
    assert resp.mimetype == "text/event-stream"
    assert resp.is_streamed
    output = resp.get_data(as_text=True)
    assert output == "event:example\ndata:thing\n\n"
    pubsub.subscribe.assert_called_with('sse')


def test_sse_object():
    assert isinstance(flask_sse.sse, flask_sse.ServerSentEventsBlueprint)
    # calling `add_url_rule` adds an entry to the `deferred_functions` list,
    # which is about the only thing we can test for
    assert len(flask_sse.sse.deferred_functions) == 1


def test_stream_channel_arg(app, mockredis, mocker):
    mock_finite_stream(mocker)
    app.config["REDIS_URL"] = "redis://localhost"
    app.register_blueprint(flask_sse.sse, url_prefix='/stream')
    client = app.test_client()
    pubsub = mockredis.pubsub.return_value
    pubsub.get_message.side_effect = [
        {
            "type": "message",
            "data": '{"data": "thing", "type": "example"}',
        }
    ]

    resp = client.get("/stream?channel=different")

    assert isinstance(resp, flask.Response)
    assert resp.mimetype == "text/event-stream"
    assert resp.is_streamed
    output = resp.get_data(as_text=True)
    assert output == "event:example\ndata:thing\n\n"
    pubsub.subscribe.assert_called_with('different')
