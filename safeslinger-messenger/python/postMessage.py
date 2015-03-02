# The MIT License (MIT)
# 
# Copyright (c) 2010-2015 Carnegie Mellon University
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from __future__ import with_statement

import base64
import logging
import os
import struct
import time

from google.appengine.api import files, urlfetch
from google.appengine.api.urlfetch_errors import DeadlineExceededError
from google.appengine.runtime import DeadlineExceededError
from google.appengine.ext import webapp
from google.appengine.ext.webapp import util

import apnsAuthToken
import gcmAuthToken
import c2dm
import c2dmAuthToken
import filestorage
import json
import registration
import random
from apns import APNs, APNsConnection, Payload, PayloadAlert
import urllib, urllib2

class PostMessage(webapp.RequestHandler):

    def post(self): 
        minlen = 4 + 4 + 20 + 4 + 1 + 4 + 1
        STR_VERSERVER = '01060000'
        INT_VERCLIENT = 0x01060000
        STR_VERCLIENT = '1.6'
 
        # must be able to query for https
        if not os.environ.has_key('HTTPS'):
            self.resp_simple(0, 'HTTPS environment variable not found')
            return

        # must be able to query for server version
        if not os.environ.has_key('CURRENT_VERSION_ID'):
            self.resp_simple(0, 'CURRENT_VERSION_ID environment variable not found')
            return

        HTTPS = os.environ.get('HTTPS', 'off')
        CURRENT_VERSION_ID = os.environ.get('CURRENT_VERSION_ID', STR_VERSERVER)

        # SSL must be enabled
        if HTTPS.__str__() != 'on':
            self.resp_simple(0, 'Secure socket required.')
            return

        # get the data from the post
        self.response.headers['Content-Type'] = 'application/octet-stream'
        data = self.request.body        
        size = str.__len__(data)

        # size check
        if size < minlen:
            self.resp_simple(0, 'Request was formatted incorrectly.')
            return

        # unpack all incoming data
        client = (struct.unpack("!i", data[0:4]))[0]
        data = data[4:]

        # client version check
        if client < INT_VERCLIENT:
            self.resp_simple(0, ('Client version mismatch; %s required.  Download latest client release first.' % STR_VERCLIENT))
            return

        server = int(CURRENT_VERSION_ID[0:8], 16)
        isProd = CURRENT_VERSION_ID[8:9] == 'p'

        # unpack all incoming data
        pos = 0        

        lenrid = (struct.unpack("!i", data[pos:(pos + 4)]))[0]
        pos = pos + 4
        retrievalId = base64.encodestring(data[pos:(pos + lenrid)])
        pos = pos + lenrid

        lenrtok = (struct.unpack("!i", data[pos:(pos + 4)]))[0]
        pos = pos + 4
        recipientToken = str(data[pos:(pos + lenrtok)])
        pos = pos + lenrtok

        lenmd = (struct.unpack("!i", data[pos:(pos + 4)]))[0]
        pos = pos + 4
        msgData = data[pos:(pos + lenmd)]
        pos = pos + lenmd

        lenfd = (struct.unpack("!i", data[pos:(pos + 4)]))[0]
        pos = pos + 4
        fileData = data[pos:(pos + lenfd)]
        pos = pos + lenfd

        # add notify type generically in 1.7 for backward-compatibility
        if len(data) >= (pos + 4):
            devtype = (struct.unpack("!i", data[pos:(pos + 4)]))[0]
        else:
            if lenrtok <= 64:
                devtype = 2  # apns was shorter
            else:
                devtype = 1  # c2dm were longer
                      
        # FILE STORAGE ===============================================================================
        if lenfd > 0:
            DATASTORE_LIMIT = 1000000  # max bytes for datastore storage
            # determine which storage method to use....
            if lenfd <= DATASTORE_LIMIT:
                # add file to data base...
                filestore = filestorage.FileStorage(id=retrievalId, data=fileData, msg=msgData, client_ver=client, sender_token=recipientToken)
            else:
                # Create the file
                blobName = files.blobstore.create(mime_type='application/octet-stream')        
                # Open the file and write to it
                with files.open(blobName, 'a') as f:
                    pos = 0
                    bdata = str(fileData[pos:(pos + 65536)])
                    pos = pos + 65536
                    while bdata:
                        f.write(bdata)
                        bdata = str(fileData[pos:(pos + 65536)])
                        pos = pos + 65536
                # Finalize the file. Do this before attempting to read it.
                files.finalize(blobName)        
                # Get the file's blob key
                blob_key = str(files.blobstore.get_blob_key(blobName)) 
                # This will only work if the file is less than 10MB. Otherwise, we send a 
                # correctly encoded multipart form and use the regular blobstore upload method. 
                filestore = filestorage.FileStorage(id=retrievalId, blobkey=blob_key, msg=msgData, client_ver=client, sender_token=recipientToken)
        else:
            filestore = filestorage.FileStorage(id=retrievalId, msg=msgData, client_ver=client, sender_token=recipientToken)
        
        # save file retrieval data and keys to datastore
        filestore.put()
        key = filestore.key()
        if not key.has_id_or_name():
            self.resp_simple(0, 'Unable to create new message.')
            return       

        # MESSAGE CONCURRENCY AVAILABILITY ===============================================================================
        # make sure the message can be retrieved before sending push notification for it.
        # this is critical to support eventual concurrency, and to prevent mis-classifying live messages as expired.
        # query for live message, using exponential backoff timeout.
        msgdata_sec = .25
        # TODO: adjust period based on deployment testing
        msgdata_tot = 0
        data_retry = True
        while data_retry and msgdata_tot < 32:  # don't wait more than 32 seconds for concurrency
            query = filestorage.FileStorage.all()
            query.filter('id =', retrievalId)
            num = query.count()
            if num >= 1:
                data_retry = False
            elif num == 0:
                msgdata_tot += msgdata_sec
                logging.info("Waiting for FileStorage concurrency - timeout: " + str(msgdata_sec))
                time.sleep(msgdata_sec)
                msgdata_sec *= 2
        # data retries have exceeded the timeout
        if data_retry:
            # TODO: model if erroring out to the client is best or if we can delay push sending
            logging.error("Continuing with push after FileStorage concurrency timed out: " + str(msgdata_sec))

        # PUSH REGISTRATION UPDATE ===============================================================================
        # TODO: this could be structured better to query one data set, rather than 2 queries
        # make sure the most recent push registration id is used 
        query = registration.Registration.all().order('-inserted')
        query.filter('registration_id =', recipientToken)
        items = query.fetch(1)  # only want the latest        
        # lookup matching key ids
        for reg_old in items:
            query2 = registration.Registration.all().order('-inserted')
            query2.filter('key_id =', reg_old.key_id)
            items2 = query2.fetch(1)  # only want the latest        
            # update registration id and device type if stored already
            for reg_new in items2:
                logging.info('Key ID found, using lookup reg (%i)%s..., not submitted reg (%i)%s...' % (reg_new.notify_type, reg_new.registration_id[0:10], devtype, recipientToken[0:10]))
                recipientToken = reg_new.registration_id
                devtype = reg_new.notify_type
                canonicalId = reg_new.canonical_id

        # otherwise, just use the submitted registration as is

        # BEGIN NOTIFY TYPES ===============================================================================
        
        # TODO: Find way to keep push secrets in memory more often, save reads from datastore.

        if devtype == 0:
            self.resp_simple(0, 'User has no push registration id.')
            return

        # C2DM ANDROID PUSH MSG ===============================================================================
        elif devtype == 1: 
            # send push message to Android service...
            sender = c2dm.C2DM()
            sender.registrationId = recipientToken
            sender.collapseKey = retrievalId
            sender.fileid = retrievalId
            
            # grab latest auth token from our cache
            query = c2dmAuthToken.C2dmAuthToken.all().order('-inserted')
            items = query.fetch(1)  # only want the latest
            num = 0
            for token in items:
                sender.clientAuth = token.token
                num = num + 1
    
            if num != 1:
                logging.error('One C2DM authorization token expected, %i found.' % num)
                self.resp_simple(0, 'Error=PushNotificationFail')
                return
    
            respMessage = sender.sendMessage()
            
            if respMessage.find('Error') != -1:
                self.resp_simple(0, (' %s') % respMessage)
                return

        # APNS APPLE PUSH MSG ===============================================================================
        elif devtype == 2: 
            # grab latest proper credential from our cache
            query = apnsAuthToken.APNSAuthToken.all()
            if isProd:
            	query.filter('lookuptag =', 'production')
            else:
            	query.filter('lookuptag =', 'test')
            	
            items = query.fetch(1)  # only want the latest
            num = 0
            for credential in items:
            	APNS_KEY = credential.apnsKey
            	APNS_CERT = credential.apnsCert
            	num = num + 1
            
            logging.info("retrievalId: " + retrievalId)
            logging.info("recipientToken: " + recipientToken)
            
            apns = None
            if isProd:
            	apns = APNs(use_sandbox=False, cert_file=APNS_CERT, key_file=APNS_KEY, enhanced=True)
            else:
            	apns = APNs(use_sandbox=True, cert_file=APNS_CERT, key_file=APNS_KEY, enhanced=True)
            
            # update badge number
            query = filestorage.FileStorage.all()
            undownloaded = False
            query.filter('sender_token =', recipientToken).filter('downloaded = ', undownloaded)
            badge = query.count()
            
            # Send a notification
            apnsmessage = {}
            apnsmessage['data'] = {}
            apnsmessage['sound'] = 'default'
            # Todo: badge should be updated with registration database
            apnsmessage['badge'] = badge
            apnsmessage['alert'] = PayloadAlert("title_NotifyFileAvailable", loc_key="title_NotifyFileAvailable")
            apnsmessage['custom'] = {'nonce': retrievalId}
            
            payload = Payload(alert=apnsmessage['alert'], sound=apnsmessage['sound'], custom=apnsmessage['custom'], badge=apnsmessage['badge'])
            
            # Status code
            # 0 No errors encountered
            # 1 Processing error
            # 2 Missing device token
            # 3 Missing topic
            # 4 Missing payload
            # 5 Invalid token size
            # 6 Invalid topic size
            # 7 Invalid payload size
            # 8 Invalid token
            # 10 Shutdown
            # 255 None (unknown)
            status = 0
            try:
            	identifier = random.getrandbits(32)
            	status = apns.gateway_server.send_notification(recipientToken, payload, identifier=identifier)
            except DeadlineExceededError:
            	logging.info("DeadlineExceededError - timeout.")
            	self.resp_simple(0, 'Error=PushNotificationFail')
            	return
               
            # received status from SSL socket, handle appropriately
            if status == 0:
                logging.info("Remote Notification successfully sent to APNS, code: " + str(status))
            elif status == 1 or status == 10:
                logging.error("Error: 500, Internal Server Error or APNS Unavailable. Our system failed. If this persists, contact support..")
                self.resp_simple(0, 'Error=PushNotificationFail')
                return
            elif status == 2 or status == 5 or status == 8:
            	self.resp_simple(0, 'Error=InvalidRegistration')
            	return
            else:
                logging.error("APNS Error: (code = %d)" % status)
                self.resp_simple(0, 'Error=PushServiceFail')
                return
            
            respMessage = struct.pack('!i', status)

        # GCM ANDROID PUSH MSG ===============================================================================
        elif devtype == 3: 
            
            # grab latest proper credential from our cache
            query = gcmAuthToken.GcmAuthToken.all().order('-inserted')
            items = query.fetch(1)  # only want the latest
            num = 0
            for token in items:
                GCM_KEY = token.gcmkey
                num = num + 1
            
            # Build payload
            if canonicalId is not None:
                proper_registration_id = canonicalId
            else:
                proper_registration_id = recipientToken

            values = {'registration_id' : proper_registration_id,
                      'data.msgid': retrievalId,
            }        

            # Build request
            headers = {'Authorization': 'key=' + GCM_KEY}
            data = urllib.urlencode(values)
            request = urllib2.Request('https://android.googleapis.com/gcm/send', data, headers)
    
            # Post
            try:
                response = urllib2.urlopen(request)
                
                # see if we have a new token to use or not...
                if 'registration_id=' in response.headers:
                    canonicalId = response.headers['registration_id']
                    logging.info("canonicalId found " + canonicalId)

                    # TODO: update registration entry with canonical id
                    # TODO: Store updated canonical for all registration matches.
                    # TODO: Avoid writing canonical Id when old one matches.
            
                respMessage = response.read()
                logging.info('%s' % respMessage)
                # When a plain-text request is successful (HTTP status code 200), the response body contains 1 or 2 lines in the form of key/value pairs. The first line is always available and its content is either id=ID of sent message or Error=GCM error code. The second line, if available, has the format of registration_id=canonical ID. The second line is optional, and it can only be sent if the first line is not an error. We recommend handling the plain-text response in a similar way as handling the JSON response:
                # 
                #     If first line starts with id, check second line:
                #         If second line starts with registration_id, gets its value and replace the registration IDs in your server database.
                #     Otherwise, get the value of Error:
                #         If it is NotRegistered, remove the registration ID from your server database.
                #         Otherwise, there is probably a non-recoverable error (Note: Plain-text requests will never return Unavailable as the error code, they would have returned a 500 HTTP status instead).

            except urllib2.HTTPError, e:
                logging.error("GCM HTTP Error: ." + str(e))
                if e.code == 500:
                    self.resp_simple(0, 'Error=PushServiceFail')
                    return
                else:
                    self.resp_simple(0, 'Error=PushNotificationFail')
                    return

            # TODO: Add handling to read plaintext response for error and canonical.

            if respMessage.find('Error') != -1:
                self.resp_simple(0, (' %s') % respMessage)
                return

        # NOT IMPLEMENTED PUSH TYPE ===============================================================================
        else: 
            self.resp_simple(0, ('Sending to device type %i not yet implemented.' % devtype))
            return

        # END NOTIFY TYPES ===============================================================================
        
        # SUCCESS RESPONSE ===============================================================================
        # file inserted and message sent
        self.response.out.write('%s' % struct.pack('!i', server))
        self.response.out.write('%s Success: %s' % (struct.pack('!i', 1), respMessage))
            

    def resp_simple(self, code, msg):
        self.response.out.write('%s%s' % (struct.pack('!i', code), msg))


def main():
    application = webapp.WSGIApplication([('/postMessage', PostMessage),
                                          ('/postFile1', PostMessage),
                                          ('/postFile2', PostMessage)],
                                         debug=True)
    util.run_wsgi_app(application)


if __name__ == '__main__':
    main()
