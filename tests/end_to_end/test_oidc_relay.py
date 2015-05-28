"""
Tests around the server OIDC relay function
"""
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import server_test
import client
import server


class OidcServerForTesting(server.Ga4ghServerForTesting):

    def getConfig(self):
        config = """
SIMULATED_BACKEND_NUM_VARIANT_SETS = 10
SIMULATED_BACKEND_VARIANT_DENSITY = 1
DATA_SOURCE = "__SIMULATED__"
DEBUG = True"""
        return config


class TestOidc(OidcServerForTesting):
    """
    An end-to-end test of the client and server
    """
    def testEndToEnd(self):
        self.client = client.ClientForTesting(self.server.getUrl())
        self.runVariantSetRequest()
        self.assertLogsWritten()
        self.runReadsRequest()
        self.runReferencesRequest()
        self.client.cleanup()

    def getServer(self):
        return OidcServerForTesting()

    def assertLogsWritten(self):
        serverOutLines = self.server.getOutLines()
        serverErrLines = self.server.getErrLines()
        clientOutLines = self.client.getOutLines()
        clientErrLines = self.client.getErrLines()

        # nothing should be written to server stdout
        self.assertEqual(
            [], serverOutLines,
            "Server stdout log not empty")

        # server stderr should log at least one response success
        responseFound = False
        for line in serverErrLines:
            if ' 200 ' in line:
                responseFound = True
                break
        self.assertTrue(
            responseFound,
            "No successful server response logged to stderr")

        # client stdout should not be empty
        self.assertNotEqual(
            [], clientOutLines,
            "Client stdout log is empty")

        # num of client stdout should be twice the value of
        # SIMULATED_BACKEND_NUM_VARIANT_SETS
        expectedNumClientOutLines = 20
        self.assertEqual(len(clientOutLines), expectedNumClientOutLines)

        # client stderr should log at least one post
        requestFound = False
        for line in clientErrLines:
            if 'POST' in line:
                requestFound = True
                break
        self.assertTrue(
            requestFound,
            "No request logged from the client to stderr")

    def runVariantSetRequest(self):
        self.runClientCmd(self.client, "variants-search -s0 -e2")

    def runReadsRequest(self):
        cmd = "reads-search --readGroupIds 'aReadGroupSet:one'"
        self.runClientCmd(self.client, cmd)

    def runReferencesRequest(self):
        referenceSetId = 'aReferenceSet'
        referenceId = 'aReferenceSet:srsone'
        cmd = "referencesets-search"
        self.runClientCmd(self.client, cmd)
        cmd = "references-search"
        self.runClientCmd(self.client, cmd)
        cmd = "referencesets-get {}".format(referenceSetId)
        self.runClientCmd(self.client, cmd)
        cmd = "references-get {}".format(referenceId)
        self.runClientCmd(self.client, cmd)
        cmd = "references-list-bases {}".format(referenceId)
        self.runClientCmd(self.client, cmd)
