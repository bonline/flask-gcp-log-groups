# Copyright 2018 Google Inc. All rights reserved.
# Use of this source code is governed by the Apache 2.0
# license that can be found in the LICENSE file.

import logging
import json
import datetime

import time
import os
from flask import Flask
from flask import request, Response, render_template, g, jsonify, current_app

from google.cloud import logging as gcplogging
from google.cloud.logging_v2.resource import Resource

from flask_gcp_log_groups.background_thread import BackgroundThreadTransport
LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

PROJECT = os.environ.get("GROUPED_LOGGING_GCP_PROJECT", os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
CLIENT = gcplogging.Client(project=PROJECT)
LOG_PREFIX = os.environ.get("GROUPED_LOGGING_LOG_PREFIX")
if hasattr(os.environ, "GCP_LOG_USE_X_HTTP_CLOUD_CONTEXT"):
    USE_X_HTTP_CLOUD_CONTEXT = os.environ.get("GCP_LOG_USE_X_HTTP_CLOUD_CONTEXT")
    TRANSPORT_PARENT = None
else:
    USE_X_HTTP_CLOUD_CONTEXT = False
    PARENT_LOG_NAME = f"{LOG_PREFIX}_request_log" if LOG_PREFIX else "request_log"
    TRANSPORT_PARENT = BackgroundThreadTransport(CLIENT, PARENT_LOG_NAME)


class GCPHandler(logging.Handler):

    def __init__(self, app, parentLogName='request', childLogName='application', 
                traceHeaderName=None,labels=None, resource=None):
        logging.Handler.__init__(self)
        self.app = app
        self.labels=labels
        self.traceHeaderName = traceHeaderName
        labels = resource.get('labels', {}) if resource else {}
        if os.environ.get("K_SERVICE"):
            RESOURCE = gcplogging.Resource(type='cloud_run_revision', labels=labels)
        else:
            RESOURCE = gcplogging.Resource(type='gae_app', labels=labels)

        self.resource = RESOURCE

        self.transport_parent = TRANSPORT_PARENT
        CHILD_LOG_NAME = f"{LOG_PREFIX}_application" if LOG_PREFIX else "application"
        self.transport_child = BackgroundThreadTransport(CLIENT, CHILD_LOG_NAME)
        self.mLogLevels = {}
        if app is not None:
            self.init_app(app)

    def emit(self, record):
        msg = self.format(record)
        record_level = record.levelno
        SEVERITY = record.levelname

        # if the current log is at a lower level than is setup, skip it
        if (record_level < LOGGER.level):
            return
        if SEVERITY not in self.mLogLevels:
            self.mLogLevels[SEVERITY] = record_level
        TRACE = None
        SPAN = None
        if (self.traceHeaderName in request.headers.keys()):
          # trace can be formatted as "X-Cloud-Trace-Context: TRACE_ID/SPAN_ID;o=TRACE_TRUE"
          rawTrace = request.headers.get(self.traceHeaderName).split('/')
          TRACE = rawTrace[0]
          TRACE = f"projects/{PROJECT}/traces/{TRACE}"
          if ( len(rawTrace) > 1) :
              SPAN = rawTrace[1].split(';')[0]

        self.transport_child.send(
                msg,
                timestamp=datetime.datetime.utcnow(),
                severity=SEVERITY,
                resource=self.resource,
                labels=self.labels,
                trace=TRACE,
                span_id=SPAN)

    def init_app(self, app):

        # capture the http_request time
        @app.before_request
        def before_request():
            g.request_start_time = time.time()
            g.request_time = lambda: "%.5fs" % (time.time() - g.request_start_time)
            gcp_handler = self
            gcp_handler.setLevel(logging.INFO)
            LOGGER.addHandler(gcp_handler)


        # always log the http_request@ default INFO
        @app.after_request
        def add_logger(response):
            TRACE = None
            SPAN = None
            if (self.traceHeaderName in request.headers.keys()):
              # trace can be formatted as "X-Cloud-Trace-Context: TRACE_ID/SPAN_ID;o=TRACE_TRUE"
              rawTrace = request.headers.get(self.traceHeaderName).split('/')
              TRACE = rawTrace[0]
              TRACE = f"projects/{PROJECT}/traces/{TRACE}"
              if ( len(rawTrace) > 1) :
                SPAN = rawTrace[1].split(';')[0]

            # https://github.com/googleapis/googleapis/blob/master/google/logging/type/http_request.proto
            REQUEST = {
                'requestMethod': request.method,
                'requestUrl': request.url,
                'status': response.status_code,
                'responseSize': response.content_length,
                'latency': g.request_time(),
                'remoteIp': request.remote_addr,
                'requestSize': request.content_length  
            }

            if 'user-agent' in request.headers:
                REQUEST['userAgent'] = request.headers.get('user-agent') 

            if request.referrer:
                REQUEST['referer'] = request.referrer

            # add the response status_code based log level
            response_severity = logging.getLevelName(logging.INFO)
            if 400 <= response.status_code < 500:
                response_severity = logging.getLevelName(logging.WARNING)
            elif response.status_code >= 500:
                response_severity = logging.getLevelName(logging.ERROR)
            if response_severity not in self.mLogLevels:
                self.mLogLevels[response_severity] = getattr(logging, response_severity)

            # find the log level priority sub-messages; apply the max level to the root log message
            severity = max(self.mLogLevels, key=self.mLogLevels.get)

            self.mLogLevels = {}
            if not USE_X_HTTP_CLOUD_CONTEXT:
                self.transport_parent.send(
                    None,
                    timestamp= datetime.datetime.utcnow(),
                    severity = severity,
                    resource=self.resource,
                    labels=self.labels,
                    trace=TRACE,
                    span_id = SPAN,
                    http_request=REQUEST)

            #response.headers['x-upstream-service-time'] = g.request_time()
            # Remove logging handler for this request
            LOGGER.removeHandler(self)
            return response
