"""
homeassistant.httpinterface
~~~~~~~~~~~~~~~~~~~~~~~~~~~

This module provides an API and a HTTP interface for debug purposes.

By default it will run on port 8080.

All API calls have to be accompanied by an 'api_password' parameter.

The api supports the following actions:

/api/state/change - POST
parameter: category - string
parameter: new_state - string
Changes category 'category' to 'new_state'

/api/event/fire - POST
parameter: event_name - string
parameter: event_data - JSON-string (optional)
Fires an 'event_name' event containing data from 'event_data'

"""

import json
import threading
import logging
from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
from urlparse import urlparse, parse_qs

import requests

from .core import EVENT_START, EVENT_SHUTDOWN, Event, CategoryDoesNotExistException

SERVER_PORT = 8080

MESSAGE_STATUS_OK = "OK"
MESSAGE_STATUS_ERROR = "ERROR"
MESSAGE_STATUS_UNAUTHORIZED = "UNAUTHORIZED"

class HTTPInterface(threading.Thread):
    """ Provides an HTTP interface for Home Assistant. """

    def __init__(self, eventbus, statemachine, api_password, server_port=SERVER_PORT, server_host=None):
        threading.Thread.__init__(self)

        # If no server host is given, accept all incoming requests
        if server_host is None:
            server_host = '0.0.0.0'

        self.server = HTTPServer((server_host, server_port), RequestHandler)

        self.server.flash_message = None
        self.server.logger = logging.getLogger(__name__)
        self.server.eventbus = eventbus
        self.server.statemachine = statemachine
        self.server.api_password = api_password

        self._stop = threading.Event()

        eventbus.listen(EVENT_START, lambda event: self.start())
        eventbus.listen(EVENT_SHUTDOWN, lambda event: self.stop())

    def run(self):
        """ Start the HTTP interface. """
        self.server.logger.info("Starting")

        while not self._stop.is_set():
            self.server.handle_request()


    def stop(self):
        """ Stop the HTTP interface. """
        self._stop.set()

        # Trigger a fake request to get the server to quit
        requests.get("http://127.0.0.1:{}".format(SERVER_PORT))

class RequestHandler(BaseHTTPRequestHandler):
    """ Handles incoming HTTP requests """

    #Handler for the GET requests
    def do_GET(self):
        """ Handle incoming GET requests. """
        write = lambda txt: self.wfile.write(txt+"\n")

        url = urlparse(self.path)

        get_data = parse_qs(url.query)

        # Verify API password
        if get_data.get('api_password', [''])[0] != self.server.api_password:
            self.send_response(200)
            self.send_header('Content-type','text/html')
            self.end_headers()

            write("<html>")
            write("<form action='/' method='GET'>")
            write("API password: <input name='api_password' />")
            write("<input type='submit' value='submit' />")
            write("</form>")
            write("</html>")


        # Serve debug URL
        elif url.path == "/":
            self.send_response(200)
            self.send_header('Content-type','text/html')
            self.end_headers()


            write("<html>")

            # Flash message support
            if self.server.flash_message is not None:
                write("<h3>{}</h3>".format(self.server.flash_message))

                self.server.flash_message = None

            # Describe state machine:
            categories = []

            write("<table>")
            write("<tr><th>Name</th><th>State</th><th>Last Changed</th></tr>")

            for category, state, last_changed in self.server.statemachine.get_states():
                categories.append(category)

                write("<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(category, state, last_changed.strftime("%H:%M:%S %d-%m-%Y")))

            write("</table>")

            # Small form to change the state
            write("<br />Change state:<br />")
            write("<form action='state/change' method='POST'>")
            write("<input type='hidden' name='api_password' value='{}' />".format(self.server.api_password))
            write("<select name='category'>")

            for category in categories:
                write("<option>{}</option>".format(category))

            write("</select>")

            write("<input name='new_state' />")
            write("<input type='submit' value='set state' />")
            write("</form>")

            # Describe event bus:
            for category in self.server.eventbus.listeners:
                write("Event {}: {} listeners<br />".format(category, len(self.server.eventbus.listeners[category])))

            # Form to allow firing events
            write("<br /><br />")
            write("<form action='event/fire' method='POST'>")
            write("<input type='hidden' name='api_password' value='{}' />".format(self.server.api_password))
            write("Event name: <input name='event_name' /><br />")
            write("Event data (json): <input name='event_data' /><br />")
            write("<input type='submit' value='fire event' />")
            write("</form>")

            write("</html>")


        else:
            self.send_response(404)


    def do_POST(self):
        """ Handle incoming POST requests. """

        length = int(self.headers['Content-Length'])
        post_data = parse_qs(self.rfile.read(length))

        if self.path.startswith('/api/'):
            action = self.path[5:]
            use_json = True

        else:
            action = self.path[1:]
            use_json = False

        self.server.logger.info(post_data)
        self.server.logger.info(action)


        # Verify API password
        if post_data.get("api_password", [''])[0] != self.server.api_password:
            self._message(use_json, "API password missing or incorrect.", MESSAGE_STATUS_UNAUTHORIZED)


        # Action to change the state
        elif action == "state/change":
            category, new_state = post_data['category'][0], post_data['new_state'][0]

            try:
                self.server.statemachine.set_state(category, new_state)

                self._message(use_json, "State of {} changed to {}.".format(category, new_state))

            except CategoryDoesNotExistException:
                self._message(use_json, "Category does not exist.", MESSAGE_STATUS_ERROR)

        # Action to fire an event
        elif action == "event/fire":
            try:
                event_name = post_data['event_name'][0]
                event_data = None if 'event_data' not in post_data or post_data['event_data'][0] == "" else json.loads(post_data['event_data'][0])

                self.server.eventbus.fire(Event(event_name, event_data))

                self._message(use_json, "Event {} fired.".format(event_name))

            except ValueError:
                # If JSON decode error
                self._message(use_json, "Invalid event received.", MESSAGE_STATUS_ERROR)


        else:
            self.send_response(404)


    def _message(self, use_json, message, status=MESSAGE_STATUS_OK):
        """ Helper method to show a message to the user. """
        log_message = "{}: {}".format(status, message)

        if status == MESSAGE_STATUS_OK:
            self.server.logger.info(log_message)
            response_code = 200

        else:
            self.server.logger.error(log_message)
            response_code = 401 if status == MESSAGE_STATUS_UNAUTHORIZED else 400

        if use_json:
            self.send_response(response_code)
            self.send_header('Content-type','application/json' if use_json else 'text/html')
            self.end_headers()

            self.wfile.write(json.dumps({'status': status, 'message':message}))

        else:
            self.server.flash_message = message

            self.send_response(301)
            self.send_header("Location", "/?api_password={}".format(self.server.api_password))
            self.end_headers()