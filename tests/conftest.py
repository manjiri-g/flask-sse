import pytest
from flask import Flask
import flask_sse


@pytest.fixture
def app():
    _app = Flask(__name__)
    _app.secret_key = "anything"
    _app.testing = True

    @_app.route("/")
    def index():
        return "index"

    return _app


@pytest.yield_fixture
def appctx(app):
    with app.test_request_context("/") as ctx:
        yield ctx


@pytest.fixture
def mockredis(mocker):
    _mr = mocker.Mock(name="mockredis")
    mocker.patch("flask_sse.StrictRedis", return_value=_mr)
    mocker.patch("flask_sse.StrictRedis.from_url", return_value=_mr)
    return _mr


@pytest.fixture
def mockpsmsgs(mocker):
    return mocker.patch.object(flask_sse.ServerSentEventsBlueprint, "pubsub_messages")
