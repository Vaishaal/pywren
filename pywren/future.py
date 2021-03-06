from __future__ import absolute_import
import boto3
import botocore
from six import reraise
import json
import base64
from threading import Thread
try:
    from six.moves import cPickle as pickle
except:
    import pickle
from pywren.wrenconfig import *
from pywren import wrenconfig, wrenutil, runtime
import enum
from multiprocessing.pool import ThreadPool
import time
from pywren import s3util
from pywren.executor import *
import logging
import botocore
import glob2
import os
from pywren import invokers
from tblib import pickling_support
pickling_support.install()

logger = logging.getLogger(__name__)

class JobState(enum.Enum):
    new = 1
    invoked = 2
    running = 3
    success = 4
    error = 5

class ResponseFuture(object):

    """
    """
    GET_RESULT_SLEEP_SECS = 4
    def __init__(self, call_id, callset_id, invoke_metadata,
                 s3_bucket, s3_prefix, aws_region):

        self.call_id = call_id
        self.callset_id = callset_id
        self._state = JobState.new
        self.s3_bucket = s3_bucket
        self.s3_prefix = s3_prefix
        self.aws_region = aws_region

        self._invoke_metadata = invoke_metadata.copy()

        self.status_query_count = 0

    def _set_state(self, new_state):
        ## FIXME add state machine
        self._state = new_state

    def cancel(self):
        raise NotImplementedError("Cannot cancel dispatched jobs")

    def cancelled(self):
        raise NotImplementedError("Cannot cancel dispatched jobs")

    def running(self):
        raise NotImplementedError()

    def done(self):
        if self._state in [JobState.success, JobState.error]:
            return True
        if self.result(check_only = True) is None:
            return False
        return True


    def result(self, timeout=None, check_only=False, throw_except=True, 
               s3_client=None):
        """


        From the python docs:

        Return the value returned by the call. If the call hasn't yet
        completed then this method will wait up to timeout seconds. If
        the call hasn't completed in timeout seconds then a
        TimeoutError will be raised. timeout can be an int or float.If
        timeout is not specified or None then there is no limit to the
        wait time.

        If the future is cancelled before completing then CancelledError will be raised.

        If the call raised then this method will raise the same exception.

        """
        if self._state == JobState.new:
            raise ValueError("job not yet invoked")

        if self._state == JobState.success:
            return self._return_val

        if self._state == JobState.error:
            if throw_except:
                raise self._exception
            else:
                return None


        logger.info("ResponseFuture.result() {} {} getting_call_status".format(self.callset_id,
                                                                           self.call_id))


        call_status = s3util.get_call_status(self.callset_id, self.call_id,
                                             AWS_S3_BUCKET = self.s3_bucket,
                                             AWS_S3_PREFIX = self.s3_prefix, 
                                             s3_client = s3_client)

        self.status_query_count += 1

        ## FIXME implement timeout
        if timeout is not None : raise NotImplementedError()

        if check_only is True:
            if call_status is None:
                return None

        while call_status is None:
            time.sleep(self.GET_RESULT_SLEEP_SECS)
            call_status = s3util.get_call_status(self.callset_id, self.call_id,
                                                 AWS_S3_BUCKET = self.s3_bucket,
                                                 AWS_S3_PREFIX = self.s3_prefix, 
                                                 s3_client = s3_client)

            self.status_query_count += 1
        logger.info("ResponseFuture.result() {} {} got call status, status_query_count={}".format(self.callset_id,
                                                                                                  self.call_id, 
                                                                                                  self.status_query_count))

        self._invoke_metadata['status_done_timestamp'] = time.time()
        self._invoke_metadata['status_query_count'] = self.status_query_count

        self.run_status = call_status # this is the remote status information
        self.invoke_status = self._invoke_metadata # local status information

        if call_status['exception'] is not None:
            # the wrenhandler had an exception
            exception_str = call_status['exception']
            print(call_status)
            exception_args = call_status['exception_args']
            if exception_args[0] == "WRONGVERSION":
                if throw_except:
                    raise Exception("Pywren version mismatch: remove expected version {}, local library is version {}".format(exception_args[2], exception_args[3]))
                return None
            elif exception_args[0] == "OUTATIME":
                if throw_except:
                    raise Exception("process ran out of time")
                return None
            else:
                if throw_except:
                    if 'exception_traceback' in call_status:
                        logger.error(call_status['exception_traceback'])
                    raise Exception(exception_str, *exception_args)
                return None

        logger.info("ResponseFuture.result() {} {} getting output".format(self.callset_id,
                                                                             self.call_id))

        call_output_time = time.time()
        call_invoker_result = pickle.loads(s3util.get_call_output(self.callset_id,
                                                                  self.call_id,
                                                                  AWS_S3_BUCKET = self.s3_bucket,
                                                                  AWS_S3_PREFIX = self.s3_prefix, 
                                                                  s3_client = s3_client))
        call_output_time_done = time.time()
        self._invoke_metadata['download_output_time'] = call_output_time_done - call_output_time

        self._invoke_metadata['download_output_timestamp'] = call_output_time_done
        call_success = call_invoker_result['success']
        logger.info("ResponseFuture.result() {} {} call_success {}".format(self.callset_id,
                                                                           self.call_id,
                                                                           call_success))



        self._call_invoker_result = call_invoker_result



        if call_success:

            self._return_val = call_invoker_result['result']
            self._state = JobState.success
            return self._return_val

        elif throw_except:

            self._exception = call_invoker_result['result']
            self._traceback = (call_invoker_result['exc_type'],
                               call_invoker_result['exc_value'],
                               call_invoker_result['exc_traceback'])

            self._state = JobState.error
            if call_invoker_result.get('pickle_fail', False):
                logging.warning("there was an error pickling. The original exception: {}\n The pickling exception: {}".format(call_invoker_result['exc_value'], str(call_invoker_result['pickle_exception'])))

                reraise(Exception, call_invoker_result['exc_value'],
                        call_invoker_result['exc_traceback'])
            else:
                # reraise the exception
                reraise(*self._traceback)
        else:
            return None  # nothing, don't raise, no value

    def exception(self, timeout = None):
        raise NotImplementedError()

    def add_done_callback(self, fn):
        raise NotImplementedError()
