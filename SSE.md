# FLASK-SSE: CONTROLLING CONNECTION AND RESOURCES

I recently played around with a Flask web application - a multi-player game - using the [flask-sse](https://flask-sse.readthedocs.io) Python package. I made some enhancements to manage connections and release resources when a player leaves the game and when the game is over. This blog is an account of the motivation behind each of the enhancements. I hope it proves useful in understanding how to control Server-sent Events (SSE) within a Flask application.

Some salient aspects of my application that relate to SSE or Redis or `flask-sse` are as follows.

## Redis PUBSUB
When one player clicks a button to roll the dice, all players see the roll right away on their browsers. Under the hood, all players' browsers connect to an SSE (Server-sent Events) stream and indirectly subscribe to a Redis PUBSUB channel. The click results in a message being published to the game's channel and an SSEvent being fired on all browsers connected to the game.

## SSE Reconnection
A retry mechanism is built into the SSE EventSource specification; the SSE client will attempt to reconnect if the connection is severed while the game is in progress. When the game is over, the server closes the SSE connection permanently and tells the client to stop connecting.

## Python Generator
The SSE server subscribes to the Redis channel when it receives an HTTP request and unsubscribes when the response is closed. The Redis PUBSUB connection remains alive for the duration of the subscription. Streaming contents in Flask makes use of a Python generator.

```python
        pubsub = self.redis.pubsub()
        pubsub.subscribe(channel)
        for pubsub_message in pubsub.listen():
            if pubsub_message['type'] == 'message':
                msg_dict = json.loads(pubsub_message['data'])
                yield Message(**msg_dict)
```

### GeneratorExit
The SSE server must unsubscribe from the channel when this loop is terminated. For starters, right before the response is closed - e.g. tab is closed - the generator is closed. This corresponds to GeneratorExit exception which presents an opportunity to reclaim resources.

```python
        pubsub = self.redis.pubsub()
        pubsub.subscribe(channel)
        try:
            for pubsub_message in pubsub.listen():
                if pubsub_message['type'] == 'message':
                    msg_dict = json.loads(pubsub_message['data'])
                    yield Message(**msg_dict)
        finally:
            pubsub.unsubscribe(channel)
```
### StopIteration
To handle the 'game over' scenario, the SSE server must actively decide to terminate the loop when no more messages are expected to be published on the channel. This also requires that the loop use `pubsub.get_message` with a `timeout`; because `pubsub.listen` will block indefinitely in this situation.

```python
        pubsub = self.redis.pubsub()
        pubsub.subscribe(channel)
        try:
            while not self.done_streaming(channel):
                pubsub_message = pubsub.get_message(timeout=15.0)
                if pubsub_message is None:  # timeout
                    continue
                if pubsub_message['type'] == 'message':
                    msg_dict = json.loads(pubsub_message['data'])
                    yield Message(**msg_dict)
        finally:
            pubsub.unsubscribe(channel)
```
### Stop reconnecting
Termination of the generator will trigger EventSource 'error' event and a reconnection attempt. In the 'game over' scenario, the SSE server tells the client to stop reconnecting with "204 NO CONTENT".

```python
        channel = request.args.get('channel') or 'sse'

        if self.done_streaming(channel):
            return current_app.response_class("", status=204)

        @stream_with_context
        def generator():
            for message in self.messages(channel=channel):
                yield str(message)

        return current_app.response_class(
            generator(),
            mimetype='text/event-stream',
        )
```
### Connection health check
Redis resources remain tied up while streaming even if the HTTP-side has disconnected if the server has no data to stream. Until then, the SSE server is not aware that the connection state has changed. This resource leak is avoided by performing a periodic health-check on the connection by sending a line that will be ignored by the EventSource engine because it begins with a colon ':'. If the `write` fails, the generator will be closed (`GeneratorExit`) giving it a chance to release Redis resources.

```python
        pubsub = self.redis.pubsub()
        pubsub.subscribe(channel)
        try:
            while not self.done_streaming(channel):
                pubsub_message = pubsub.get_message(timeout=15.0)
                if pubsub_message is None:  # timeout
                    yield ":Health-check\n"
                elif pubsub_message['type'] == 'message':
                    msg_dict = json.loads(pubsub_message['data'])
                    yield Message(**msg_dict)
        finally:
            pubsub.unsubscribe(channel)
```
## Control messages
The application can wield more control over the generator by publishing a 'control' message that triggers immediate termination of loop or a health-check. (The application can also use EventSource events (i.e. data messages) for health-checking and/or telling the remote side to close the connection.)

```python
        pubsub = self.redis.pubsub()
        pubsub.subscribe(channel)
        health_check = ":Health-check\n"
        try:
            while not self.done_streaming(channel):
                pubsub_message = pubsub.get_message(timeout=15.0)
                if pubsub_message is None:  # timeout
                    yield health-check
                elif pubsub_message['type'] == 'message':
                    msg_dict = json.loads(pubsub_message['data'])
                    command = msg_dict.get('sse-control')
                    if command is None:
                        yield Message(**msg_dict)
                    elif command == 'health-check':
                        yield health_check
                    elif command == 'disconnect':
                        break
        finally:
            pubsub.unsubscribe(channel)
```

## Configuration
Ultimate control over these behaviors comes from configuration parameters or subclassing.
  - SSE_REDIS_CHANNEL_KEY_PREFIX
  - SSE_HEALTH_CHECK
  - SSE_TIMEOUT

## Code
Flask SSE Blueprint for connection and resource management and control: https://github.com/manjiri-g/flask-sse/tree/sse-manager

```python
    def done_streaming(self, channel='sse'):
        prefix = current_app.config.get("SSE_REDIS_CHANNEL_KEY_PREFIX")
        if prefix is None:
            return None  # never done streaming
        key = prefix + channel
        return not self.redis.exists(key)  # not done streaming if key exists

    def publish(self, data, type=None, id=None, retry=None, channel='sse'):
        message = Message(data, type=type, id=id, retry=retry)
        msg_json = json.dumps(message.to_dict())
        return self.redis.publish(channel=channel, message=msg_json)

    def control(self, command, channel='sse'):
        msg_dict = {'sse-control': command}
        msg_json = json.dumps(msg_dict)
        return self.redis.publish(channel=channel, message=msg_json)

    def blocks(self, channel='sse'):
        health_check = current_app.config.get("SSE_HEALTH_CHECK")
        if health_check is not None:
            health_check = ":{}\n".format(health_check)

        timeout = current_app.config.get("SSE_TIMEOUT")
        if timeout is None and (health_check or self.done_streaming(channel) is not None):
            timeout = 15.0

        pubsub = self.redis.pubsub()
        pubsub.subscribe(channel)
        try:
            while not self.done_streaming(channel):
                pubsub_message = pubsub.get_message(timeout=timeout)
                if pubsub_message is None:
                    if health_check:
                        yield health_check
                elif pubsub_message['type'] == 'message':
                    msg_dict = json.loads(pubsub_message['data'])
                    command = msg_dict.get('sse-control')
                    if command is None:
                        yield str(Message(**msg_dict))
                    elif command == 'health-check':
                        if health_check:
                            yield health_check
                    elif command == 'disconnect':
                        break
        finally:
            pubsub.unsubscribe(channel)

    def stream(self):
        channel = request.args.get('channel') or 'sse'

        if self.done_streaming(channel):
            return current_app.response_class("", status=204)

        @stream_with_context
        def generator():
            for block in self.blocks(channel=channel):
                yield block

        return current_app.response_class(
            generator(),
            mimetype='text/event-stream',
        )
```


To recap, resources allocated when SSE streaming begins must be released when streaming stops or is interrupted. SSE specification has a builtin mechanism to resend request. Consequently, the server must manage resource allocation on a per-request basis. SSE server can tell the client to stop reconnecting and avoid allocating resources if there is no streaming anticipated. The SSE server needs to perform a health-check of the connnection if data messages are not being generated. The application can control the stream by sending in-band 'sse-control' messages. Control message 'disconnect' terminates the stream and control message 'health-check' immediately checks the connection. A health-check does not trigger an SSE event on the client side.

A more detailed analysis of connection state changes is as follows.

## Publishing a message

   How messages are published to Redis PUBSUB is application-specific. Each message is seen by all subscribers' (same channel) streaming message generators; thereby firing events on multiple SSE clients. The connection opened for publishing a message to Redis does not tie up resources. In the following example, clicking a button on a non-SSE web client results in a message being published to a Redis channel. An SSE event will be fired on all SSE clients that have connected to the corresponding EventSource since the SSE server has subscribed to the Redis channel on behalf of each of them.


```
        Redis                    Web/SSE              Non-SSE            SSE
        Server                   Server               Client             Clients
          |                        |                   |                  :
          + <-- PUBLISH ---------- + <-- Request ----- + submit           :
          |\                       | --- Response ---> +                  :
          | \                      |                   |                  :
          +  \                     |                                      :
           \  +--- messageN -----> | --- data: messageN ----------------> + event
            \                      |                                      :
             +---- messageN -----> | --- data: messageN ----------------> + event
                                   |                                      :
```
Figure 1. Publishing a message

## Establishing an SSE connection

Connection initiated by client (JavaScript):
```javascript
source = new EventSource("http://hostname/stream");
```

Each message published to Redis PUBSUB will result in an event of type "message" or a user-defined type. The health-check does not result in an event; it gets ignored by the client because the line begins with a colon ':'.


```
                        Redis                    SSE                                     SSE
                        Server                   Server                                  Client#N
                          |                        |                                      |
                          | <-- SUBSCRIBE -------- + <-- GET /stream -------------------- + new EventSource
                          |                        |                                      |
                          |                        |                                      |
     PUBLISH message1 --> + ---------------------> + --- 200 OK ------------------------> + event 'open'
                          |                        | --- text/event-stream -------------> |
                          |                        | --- data: message1 ----------------> + event 'message' or user-defined
                          |                        |                                      |
     PUBLISH message2 --> + ---------------------> | --- data: message2 ----------------> + event 'message' or user-defined
                          |                        |                                      |
                          |                timeout | --- :Health-check -----------------> |
                          |                        |                                      |
```
Figure 2. SSE connection

## Closing the connection

Either the SSE client or the server can decide to close the connection.

### Client disconnects
`EventSource.close()` or closing the tab/window will attempt to close the TCP connection to the SSE server.

```
                        Redis                    SSE                                     SSE
                        Server                   Server                                  Client#N
                          |                        |                                      |
                          |                     (1)| <-- TCP FIN ------------------------ + close
                          |                        | --- TCP ACK -----------------------> |
                          |                        |                                      |
                          |        message/timeout + --- TCP PSH -----------------------> | no event
                          | <-- UNSUBSCRIBE ------ + <-- TCP RST ------------------------ |(2)
                          |                        |                                      |

```
Figure 3. Client disconnects

   (1) TCP FIN from client changes the state of the TCP connection from ESTABLISHED to CLOSE-WAIT on the server side.
   (2) SSE server detects this change in state when the message generator attempts to send a packet by way of TCP RST.
       Note: If there is no timeout+health-check this change in state can only be detected if a message is published on the channel. Resources on both sides (HTTP and Redis) remain tied up until the SSE server or redis server is restarted. Using `pubsub.get_message` (instead of `pubsub.listen`) allows the SSE server to periodically perform a health-check on the connection. TCP RST from client changes the state of the TCP connection from CLOSE-WAIT to CLOSED, releasing the HTTP side resources. The SSE server closes the message generator (`GeneratorExit`) and releases the Redis side resources.

### Server disconnects

   When the application decides to end streaming on this channel, it tells the message generator to stop by sending an 'sse-control' message. Redis PUBSUB channel messages returned by `pubsub.listen` or `pubsub.get_message` types are: 'unsubscribe', 'subscribe', or 'message'. The latter type represents published messages which can either be of (sub) type 'data' or 'sse-control'. Messages of (sub) type 'data' become Server-sent Events. SSE-control message type 'disconnect' tells the generator to stop interation and release Redis side resources. SSE server closes the TCP connection with client, releasing HTTP-side resources.

   An SSE event of type 'error' gets fired on the client side. It is followed by an attempt to reconnect to the server.

```

                        Redis                    SSE                                     SSE
                        Server                   Server                                  Client#N
                          |                        |                                      |
     PUBLISH command ---> + ---------------------> + --- TCP FIN -----------------------> + event 'error'
                          | <-- UNSUBSCRIBE ------ | <-- TCP ACK ------------------------ |
                          |                        | <-- TCP FIN ------------------------ |
                          |                        | --- TCP ACK -----------------------> |
                          |                        |                                      |
                          |                        |                               ...... | retry connect
                          |                        |                                      |

```
Figure 4. Server disconnects

## Done streaming
   An SSE client will not attempt to reconnect if the connection was closed by the client or if the server tells it not to reconnect. An application that decides to tell the client not to reconnect does so by causing the message generator check for 'done streaming' to pass.

   The Redis-based implementation of 'done streaming' involves checking the database for presence of a key, based on configuration. Configuration parameter specifying a prefix tells the server to check the Redis database for a key named prefix+channel. This prefix can be ''. The default, when the parameter SSE_REDIS_CHANNEL_KEY_PREFIX is absent from configuration, is None i.e. never stop streaming. There is a one-to-one correspondence between this key and channel. A Redis PUBSUB channel cannot be deleted; a subscriber can always subscribe to a channel whether a publisher exists or not. The 'done streaming' check avoids resources being tied up when the client attempts to connect to a channel where no messages are expected to be published anymore.

### Server done streaming after starting response (200 OK)
   The message generator stops iteration and releases Redis PUBSUB resources if it detects the 'done streaming' condition when a message gets published or a timeout occurs. In other words, the server disconnects (similar to sse-control:disconnect). The SSE client tries to reconnect.

```
                        Redis                    SSE                                     SSE
                        Server                   Server                                  Client#N
                          |                        |                                      |
                          |        message/timeout |                                      |
                          |         done streaming |                                      |
                          | <-- UNSUBSCRIBE ------ |                                      |
                          |                        |                                      |
                          |                        + --- TCP FIN -----------------------> + event 'error'
                          |                        | <-- TCP ACK ------------------------ |
                          |                        | <-- TCP FIN ------------------------ |
                          |                        | --- TCP ACK -----------------------> |
                          |                        |                                      |
                          |                        |                               ...... | retry connect
                          |                        |                                      |

```
Figure 5. Server done streaming, disconnects

### Client (re)connects after server is done streaming
   If the server detects the 'done streaming' condition when the client connects, it will not send a streaming response or allocate Redis PUBSUB resources. Instead, it will respond with '204 NO CONTENT' which tells the client to stop reconnecting. An 'error' event is fired on the client side.

```
                        Redis                    SSE                                     SSE
                        Server                   Server                                  Client#N
                          |                        |                                      |
                          |                        |                                      |
                          |                        + <-- GET /stream -------------------- + connect/retry connect
                          |         done streaming + --- 204 NO CONTENT ----------------> + event 'error'
                          |                        |                                      | stop retry connect
                          |                        |                                      |
```
Figure 6. Server tells client to stop reconnecting

## Conclusion

SSE connection state changes can be controlled by either the client or by server. Redis PUBSUB is a convenient method for implementing SSE using Flask. See https://github.com/manjiri-g/flask-sse/tree/sse-manager for flexible mechanisms to manage and control the SSE connection and Redis resources using ``ManagedServerSentEventsBlueprint``.

## Appendix

### Usage
Python server
```python
    from flask import Flask
    from flask_sse import msse

    app = Flask(__name__)
    app.config["SSE_REDIS_URL"] = "redis://localhost"
    app.config["SSE_REDIS_CHANNEL_KEY_PREFIX"] = ""
    app.config["SSE_HEALTH_CHECK"] = "HC"
    app.config["SSE_TIMEOUT"] = 20.0
    app.register_blueprint(msse, url_prefix='/stream')
```
Javascript client
```javascript
    var source = new EventSource("{{ url_for('msse.stream') }}");
```

### TCP tools
```
lsof -i4tcp:6379,8080 -nP
tcpdump -lnnA port 6379 or port 8080
```
### redis-cli tools
```
PUBLISH sse "{\"data\": \"ZDEFAULT\"}"
PUBLISH sse "{\"data\": \"ZDATA\", \"type\": \"message\"}"
publish sse "{\"data\": \"ZEVENTDATA\", \"type\": \"zevent\"}"

publish sse "{\"sse-control\": \"health-check\"}"
publish sse "{\"sse-control\": \"disconnect\"}"

SET sse keep-streaming
DEL sse

KEYS *
EXISTS sse

PUBSUB CHANNELS
PUBSUB NUMSUB sse
CLIENT LIST TYPE PUBSUB

MONITOR
```
