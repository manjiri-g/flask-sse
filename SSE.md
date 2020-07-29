FLASK-SSE: RELEASING RESOURCES

I used the `flask-sse` Python package recently. It met the basic requirement of my web application - a multi-player game. When one player clicks a button to roll the dice, all players see the roll right away. Under the hood, all players' browsers connect to an SSE stream and subscribe to a Redis PUBSUB channel. The click results in a message being published to the channel and an Server-sent Event being fired on all browsers.

A retry mechanism is built into the EventSource specification - the SSE client will attempt to reconnect if the connection is severed while the game is in progress. When the game is over, the server closes the SSE connection permanently and tells the client to stop connecting.

The SSE server subscribes to the Redis channel when it receives an HTTP request and unsubscribes when the response is closed. The Redis PUBSUB connection remains alive for the duration of the subscription. Streaming contents in Flask makes use of a generator. A simplified description of this generator is that it `yields` messages from a loop. 

```
        pubsub = self.redis.pubsub()
        pubsub.subscribe(channel)
        for pubsub_message in pubsub.listen():
            if pubsub_message['type'] == 'message':  # other types are: 'subscribe', 'unsubscribe', etc.
                msg_dict = json.loads(pubsub_message['data'])
                yield Message(**msg_dict)
```

The SSE server must unsubscribe from the channel when this loop is terminated. For starters, right before the response is closed - e.g. tab is closed - the generator is closed. This corresponds to GeneratorExit exception.

```
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

To handle the 'game over' scenario, the SSE server must actively decide to terminate the loop when no more messages are expected to be published on the channel. This also requires that the loop use `pubsub.get_message()` with a timeout; because `pubsub.listen()` will block indefinitely in this situation.

```
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

Termination of the generator will trigger EventSource 'error' event and a reconnection attempt. In the 'game over' scenario, the SSE server tells the client to stop reconnecting with "204 NO CONTENT".

```
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

Redis resources remain tied up while streaming even if the HTTP-side has disconnected if the server has no data to stream. Until then, the SSE server is not aware that the connection state has changed. This resource leak is plugged by performing a periodic health-check on the connection by sending a line that will be ignored by the EventSource engine because it begins with a colon ':'. If the `write` fails, the generator will be closed (`GeneratorExit`) giving it a chance to release Redis resources.

```
        pubsub = self.redis.pubsub()
        pubsub.subscribe(channel)
        try:
            while not self.done_streaming(channel):
                pubsub_message = pubsub.get_message(timeout=15.0)
                if pubsub_message is None:  # timeout
                    yield ":Health-check\n"
                    continue
                if pubsub_message['type'] == 'message':
                    msg_dict = json.loads(pubsub_message['data'])
                    yield Message(**msg_dict)
        finally:
            pubsub.unsubscribe(channel)
```

The application can wield more control over the generator by sending a 'control' message that triggers immediate termination of loop or a health-check. (The application can also use EventSource events (i.e. data messages) for health-checking and/or telling the remote side to close the connection.)

```
        pubsub = self.redis.pubsub()
        pubsub.subscribe(channel)
        health_check = ":Health-check\n"
        try:
            while not self.done_streaming(channel):
                pubsub_message = pubsub.get_message(timeout=15.0)
                if pubsub_message is None:  # timeout
                    yield health-check
                    continue
                if pubsub_message['type'] == 'message':
                    msg_dict = json.loads(pubsub_message['data'])
                    command = msg_dict.get('sse-control')
                    if command is None:
                        yield Message(**msg_dict)
                        continue
                    if command == 'health-check':
                        yield health_check
                        continue
                    if command == 'disconnect':
                        break
        finally:
            pubsub.unsubscribe(channel)
```

Ultimate control over these behaviors comes from configuration parameters or subclassing.
  SSE_REDIS_CHANNEL_KEY_PREFIX
  SSE_HEALTH_CHECK
  SSE_TIMEOUT

```
    def publish(self, data, type=None, id=None, retry=None, channel='sse'):
        message = Message(data, type=type, id=id, retry=retry)
        msg_json = json.dumps(message.to_dict())
        return self.redis.publish(channel=channel, message=msg_json)

    def control(self, command, channel='sse'):
        msg_dict = {'sse-control': command}
        msg_json = json.dumps(msg_dict)
	return self.redis.publish(channel=channel, message=msg_json)

    def done_streaming(self, channel='sse'):
        prefix = current_app.config.get("SSE_REDIS_CHANNEL_KEY_PREFIX")
        if prefix is None:
            return None  # never done streaming

        key = prefix + channel
        return not self.redis.exists(key)  # done streaming if key does not exist

    def messages(self, channel='sse'):
        health_check = current_app.config.get("SSE_HEALTH_CHECK")
        if health_check is not None:
            health_check = ":{}\n".format(health_check)

        timeout = current_app.config.get("SSE_TIMEOUT")
        if timeout == 0:
            raise ValueError
        if (health_check or self.done_streaming(channel) is not None) and timeout is None:
            timeout = 15.0  # Need a timeout health-check or check if done streaming

        pubsub = self.redis.pubsub()
        pubsub.subscribe(channel)
        try:
            while not self.done_streaming(channel):
                pubsub_message = pubsub.get_message(timeout=timeout)
                if pubsub_message is None:  # timeout
                    if health_check:
                        yield health_check
                    continue
                if pubsub_message['type'] == 'message':
                    msg_dict = json.loads(pubsub_message['data'])
                    command = msg_dict.get('sse-control')
                    if command is None:
                        yield Message(**msg_dict)
                        continue
                    if command == 'health-check':
                        if health_check:
                            yield health_check
                        continue
                    if command == 'disconnect':
                        break
        finally:
            pubsub.unsubscribe(channel)
```

To recap, resources allocated when SSE streaming begins must be released when streaming stops or is interrupted. SSE specification has a builtin mechanism to reconnect and resend the HTTP request, if streaming is interrupted. Consequently, the server must manage resource allocation on a per request basis. SSE server can tell the client to stop reconnecting and avoid allocating resources if there is no streaming anticipated. The SSE server needs to perform a periodic health-check of the connnection if it cannot rely on data messages being generated. The application can control the stream by sending in-band 'sse-control' messages. Control message 'disconnect' terminates the stream and control message 'health-check' immediately checks the connection. A health-check does not trigger an SSE event on the client side.

The code for Redis resource management for Flask-sse can be found here.

A more detailed analysis of connection state changes and corresponding events follows.

# Publishing a message

   How messages are published to Redis PUBSUB is application-specific. Each message is seen by all subscribers' (same channel) streaming message generators; thereby firing events on multiple SSE clients. The connection opened for publishing a message to Redis does not tie up resources. In the following example, clicking a button on a non-SSE web client results in a message being published to a Redis channel. An SSE event will be fired on all SSE clients that have connected to the corresponding EventSource since the SSE server has subscribed to the Redis channel on behalf of each of them.

Figure 1. Publishing a message

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

# Establishing an SSE connection

New connection initiated by client with JavaScript:
```
source = new EventSource("http://hostname/stream");
```

Each message published to Redis PUBSUB will result in an event of type "message" or a user-defined type. The health-check does not result in an event; it gets ignored by the client because the line begins with a colon ':'.

Figure 2. SSE connection
			    

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

# Disconnect

Either the SSE client or the server can decide to close the connection.

## Client disconnects
   `EventSource.close()` or closing the tab/window will attempt to close the TCP connection to the SSE server

Figure 3. Client disconnects

                        Redis                    SSE                                     SSE
                        Server                   Server                                  Client#N
                          |                        |                                      |
                          |                     (1)| <-- TCP FIN ------------------------ + close
                          |                        | --- TCP ACK -----------------------> |
                          |                        |                                      |
                          |        message/timeout + --- TCP PSH -----------------------> | no event
                          | <-- UNSUBSCRIBE ------ + <-- TCP RST ------------------------ |(2)
                          |                        |                                      |

   (1) TCP FIN from client changes the state of the TCP connection from ESTABLISHED to CLOSE-WAIT on the server side.
   (2) SSE server detects this change in state when the message generator attempts to send a packet by way of TCP RST.
       Note: If there is no timeout+health-check this change in state can only be detected if a message is published on the channel. Resources on both sides (HTTP and Redis) remain tied up until the SSE server or redis server is restarted. Using `pubsub.get_message` (instead of `pubsub.listen`) allows the SSE server to periodically perform a health-check on the connection. TCP RST from client changes the state of the TCP connection from CLOSE-WAIT to CLOSED, releasing the HTTP side resources. The SSE server closes the message generator (`GeneratorExit`) and releases the Redis side resources.

## Server disconnects

   When the application decides to end streaming on this channel, it tells the message generator to stop by sending an 'sse-control' message. Redis PUBSUB channel messages returned by `pubsub.listen` or `pubsub.get_message` types are: 'unsubscribe', 'subscribe', or 'message'. The latter type represents published messages which can either be of (sub) type 'data' or 'sse-control'. Messages of (sub) type 'data' become Server-sent Events. SSE-control message type 'disconnect' tells the generator to stop interation and release Redis side resources. SSE server closes the TCP connection with client, releasing HTTP-side resources.

   An SSE event of type 'error' gets fired on the client side. It is followed by an attempt to reconnect to the server.
   
Figure 4. Server disconnects

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

# Done streaming
   An SSE client will not attempt to reconnect if the connection was closed by the client or if the server tells it not to reconnect. An application that decides to tell the client not to reconnect does so by causing the message generator check for 'done streaming' to return `True`.

   The Redis-based implementation of 'done streaming' involves checking the database for presence of a key, based on configuration. Configuration parameter specifying a prefix tells the server to check the Redis database for a key named prefix+channel. This prefix can be ''. The default, when the parameter SSE_REDIS_CHANNEL_KEY_PREFIX is absent from configuration, is None i.e. never stop streaming. There is a one-to-one correspondence between this key and channel. A Redis PUBSUB channel cannot be deleted; a subscriber can always subscribe to a channel whether a publisher exists or not. The 'done streaming' check avoids resources being tied up when the client attempts to connect to a channel where no messages are expected to be published anymore.

## Server done streaming after 200 OK
   The message generator stops iteration and releases Redis PUBSUB resources if it detects the 'done streaming' condition when a message gets published or a timeout occurs. In other words, the server disconnects (similar to sse-control:disconnect). The SSE client tries to reconnect.

Figure 5. Server done streaming, disconnects


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


## Server done streaming and client (re)connects
   If the server detects the 'done streaming' condition when the client connects, it will not send a streaming response or allocate Redis PUBSUB resources. Instead, it will respond with '204 NO CONTENT' which tells the client to stop reconnecting. An 'error' event is fired on the client side.

Figure 6. Server tells client to stop reconnecting

                        Redis                    SSE                                     SSE
                        Server                   Server                                  Client#N
                          |                        |                                      |
                          |                        |                                      |
                          |                        + <-- GET /stream -------------------- + connect/retry connect
                          |         done streaming + --- 204 NO CONTENT ----------------> + event 'error'
                          |                        |                                      | stop retry connect
                          |                        |                                      |

# Conclusion

Redis PUBSUB is a convenient method for implementing SSE using Flask. Class `ServerSentEventsManagerBlueprint` provides mechanisms to manage the SSE connection and Redis resources.