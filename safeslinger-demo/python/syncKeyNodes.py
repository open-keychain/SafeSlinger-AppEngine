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

import base64
import json
import logging
import os
import struct

from google.appengine.ext import webapp
from google.appengine.ext.webapp import util

import member


class SyncKeyNodes(webapp.RequestHandler):

    isJson = False
   
    def post(self):
        self.response.headers.add_header("Access-Control-Allow-Origin", "*")
        
        header = self.request.headers['Content-Type']
        logging.debug("Content-Type: '%s'" % header)
        if (str(header).startswith('text/plain')):
            self.isJson = True
            # set response to json
            self.response.headers['Content-Type'] = 'text/plain'
            data_dict = json.loads(self.request.body)
        else:
            self.response.headers['Content-Type'] = 'application/octet-stream'
            
        STR_VERSERVER = '01060000'
        INT_VERCLIENT = 0x01060000
        STR_VERCLIENT = '1.6'

        if not os.environ.has_key('HTTPS'):
            self.resp_simple(0, 'HTTPS environment variable not found')
            return

        if not os.environ.has_key('CURRENT_VERSION_ID'):
            self.resp_simple(0, 'CURRENT_VERSION_ID environment variable not found')
            return

        HTTPS = os.environ.get('HTTPS', 'off')
        CURRENT_VERSION_ID = os.environ.get('CURRENT_VERSION_ID', STR_VERSERVER)
        
        # SSL must be enabled
        if HTTPS.__str__() != 'on':
            self.resp_simple(0, 'Secure socket required.')
            return

        minlen = 4 + 4
                
        # get the data from the post
        data = self.request.body
        logging.debug("in body '%s'" % data)
    
        size = str.__len__(data)
        logging.debug("in size %d" % size)

        if size < minlen:
            self.resp_simple(0, 'Request was formatted incorrectly.')
            return
         
        # unpack all incoming data
        server = int(CURRENT_VERSION_ID[0:8], 16)
        
        if self.isJson:
            client = int(data_dict['ver_client'], 10)
        else:
            client = (struct.unpack("!i", data[0:4]))[0]
        logging.debug("in client %d" % client)
 
        if self.isJson:
            usrid = int(data_dict['usrid'], 10)
        else:
            usrid = (struct.unpack("!i", data[4:8]))[0]
        logging.debug("in usrid %d" % usrid)
        
        expectedsize = 4 + 4

        postSelf = False
        if self.isJson:
            if 'keynode_b64' in data_dict:
                usridpost = int(data_dict['usridpost'], 10)
                key_node = base64.decodestring(data_dict['keynode_b64'])
                postSelf = True
        else:
            if size > expectedsize:
                usridpost = (struct.unpack("!i", data[8:12]))[0]
                sizeData = (struct.unpack("!i", data[12:16]))[0]
                logging.debug("in sizeData %i" % sizeData)
                key_node = (struct.unpack(str(sizeData) + "s", data[16:16 + sizeData]))[0]            
                postSelf = True
        if postSelf:
            logging.debug("in usridpost %i" % usridpost)
            logging.debug("in key_node '%s'" % key_node)
                   
        # client version check
        if client < INT_VERCLIENT:
            self.resp_simple(0, ('Client version mismatch; %s required.  Download latest client release first.' % STR_VERCLIENT))
            return        

        # verify you have an existing group
        query = member.Member.all()
        query.filter('usr_id =', usrid)
        num = query.count()
        
        # requesting user exists
        if num == 1:
            mem = query.get()
            
            # verify...
            if postSelf:
                query = member.Member.all()
                query.filter('usr_id =', usridpost)
                num = query.count()
                # user exists for updating node
                if num == 1:
                    mem_other = query.get()
                    mem_other.key_node = key_node
                    mem_other.put()
                    key = mem_other.key()
                    if not key.has_id_or_name():
                        self.resp_simple(0, 'Unable to update user.')
                        return       
                else:
                    self.resp_simple(0, 'user %i does not exist for update' % (usridpost))
                    return   
                                
            # version
            if not self.isJson:
                self.response.out.write('%s' % struct.pack('!i', server))
            logging.debug("out server %i" % server)

            # node data
            mem = query.get()
            if mem.key_node != None:
                if not self.isJson:
                    self.response.out.write('%s' % struct.pack('!i', num))
                logging.debug("out total key_nodes %i" % num)
                length = str.__len__(mem.key_node)
                if self.isJson:            
                    json.dump({"ver_server":str(server), "node_total":str(num), "keynode_b64":base64.encodestring(mem.key_node) }, self.response.out)
                else:
                    self.response.out.write('%s%s' % (struct.pack('!i', length), mem.key_node))                    
                logging.debug("out mem.key_node length %i" % length)
                logging.debug("out mem.key_node '%s'" % mem.key_node)
            else:
                if self.isJson:            
                    json.dump({"ver_server":str(server), "node_total":str(0) }, self.response.out)
                else:
                    self.response.out.write('%s' % struct.pack('!i', 0))
                logging.debug("out total key_nodes %i" % 0)
        
        else:
            self.resp_simple(0, 'user %i does not exist' % (usrid))
            return      
    

    def resp_simple(self, code, msg):
        if self.isJson:            
            json.dump({"err_code":str(code), "err_msg":str(msg)}, self.response.out)
        else:
            self.response.out.write('%s%s' % (struct.pack('!i', code), msg))
        if code == 0:
            logging.error(msg)


def main():
    STR_VERSERVER = '01060000'
    CURRENT_VERSION_ID = os.environ.get('CURRENT_VERSION_ID', STR_VERSERVER)
    isProd = CURRENT_VERSION_ID[8:9] == 'p'
    # Set the logging level in the main function
    if isProd:
        logging.getLogger().setLevel(logging.INFO)
    else:
        logging.getLogger().setLevel(logging.DEBUG)

    application = webapp.WSGIApplication([('/syncKeyNodes', SyncKeyNodes),
                                     ],
                                     debug=True)
    util.run_wsgi_app(application)


if __name__ == '__main__':
    main()

