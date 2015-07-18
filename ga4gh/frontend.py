"""
The Flask frontend for the GA4GH API.

TODO Document properly.
"""
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import os
import datetime
import socket
import urlparse
import functools

import flask
import flask.ext.cors as cors
import humanize
import werkzeug
import oic
import oic.oauth2
import oic.oic.message as message
import requests

import ga4gh
import ga4gh.backend as backend
import ga4gh.datamodel as datamodel
import ga4gh.protocol as protocol
import ga4gh.exceptions as exceptions


MIMETYPE = "application/json"
SEARCH_ENDPOINT_METHODS = ['POST', 'OPTIONS']
SECRET_KEY_LENGTH = 24

app = flask.Flask(__name__)
assert not hasattr(app, 'urls')
app.urls = []


class NoConverter(werkzeug.routing.BaseConverter):
    """
    A converter that allows the routing matching algorithm to not
    match on certain literal terms

    This is needed because if there are e.g. two routes:

    /<version>/callsets/search
    /<version>/callsets/<id>

    A request for /someVersion/callsets/search will get routed to
    the second, which is not what we want.
    """
    def __init__(self, map, *items):
        werkzeug.routing.BaseConverter.__init__(self, map)
        self.items = items

    def to_python(self, value):
        if value in self.items:
            raise werkzeug.routing.ValidationError()
        return value


app.url_map.converters['no'] = NoConverter


class Version(object):
    """
    A major/minor/revision version tag
    """
    currentString = "current"

    @classmethod
    def isCurrentVersion(cls, versionString):
        if versionString == cls.currentString:
            return True
        return (Version.parseString(versionString) ==
                Version.parseString(protocol.version))

    @classmethod
    def parseString(cls, versionString):
        versions = versionString.strip('vV').split('.')
        return Version(*versions)

    def __init__(self, major, minor, revision):
        self.version = (major, minor, revision)

    def __cmp__(self, other):
        return cmp(self.version, other.version)

    def __hash__(self):
        return hash(self.version)

    def __eq__(self, other):
        return self.version == other.version

    def __ne__(self, other):
        return not self.__eq__(other)

    @classmethod
    def getVersionForUrl(cls, versionString):
        """
        Returns the specfied version string in a form suitable for using
        within a URL. This involved prefixing with 'v'.
        """
        ret = versionString
        if not ret.startswith("v"):
            ret = "v{}".format(versionString)
        return ret


class ServerStatus(object):
    """
    Generates information about the status of the server for display
    """
    def __init__(self):
        self.startupTime = datetime.datetime.now()

    def getConfiguration(self):
        """
        Returns a list of configuration (key, value) tuples
        that are useful for users to view on an information page.
        Note that we should be careful here not to leak sensitive
        information. For example, keys and paths of data files should
        not be returned.
        """
        # TODO what other config keys are appropriate to export here?
        keys = [
            'DEBUG', 'REQUEST_VALIDATION', 'RESPONSE_VALIDATION',
            'DEFAULT_PAGE_SIZE', 'MAX_RESPONSE_LENGTH',
        ]
        return [(k, app.config[k]) for k in keys]

    def getPreciseUptime(self):
        """
        Returns the server precisely.
        """
        return self.startupTime.strftime("%H:%M:%S %d %b %Y")

    def getNaturalUptime(self):
        """
        Returns the uptime in a human-readable format.
        """
        return humanize.naturaltime(self.startupTime)

    def getProtocolVersion(self):
        """
        Returns the GA4GH protocol version we support.
        """
        return protocol.version

    def getServerVersion(self):
        """
        Returns the software version of this server.
        """
        return ga4gh.__version__

    def getUrls(self):
        """
        Returns the list of (httpMethod, URL) tuples that this server
        supports.
        """
        app.urls.sort()
        return app.urls

    def getDatasetIds(self):
        """
        Returns the list of datasetIds for this backend
        """
        return app.backend.getDatasetIds()

    def getVariantSets(self, datasetId):
        """
        Returns the list of variant sets for the dataset
        """
        return app.backend.getDataset(datasetId).getVariantSets()

    def getReadGroupSets(self, datasetId):
        """
        Returns the list of ReadGroupSets for the dataset
        """
        return app.backend.getDataset(datasetId).getReadGroupSets()

    def getReferenceSets(self):
        """
        Returns the list of ReferenceSets for this server.
        """
        return app.backend.getReferenceSets()


def configure(configFile=None, baseConfig="ProductionConfig",
              port=8000, extraConfig={}):
    """
    TODO Document this critical function! What does it do? What does
    it assume?
    """
    configStr = 'ga4gh.serverconfig:{0}'.format(baseConfig)
    app.config.from_object(configStr)
    if os.environ.get('GA4GH_CONFIGURATION') is not None:
        app.config.from_envvar('GA4GH_CONFIGURATION')
    if configFile is not None:
        app.config.from_pyfile(configFile)
    app.config.update(extraConfig.items())
    # Setup file handle cache max size
    datamodel.fileHandleCache.setMaxCacheSize(
        app.config["FILE_HANDLE_CACHE_MAX_SIZE"])
    # Setup CORS
    cors.CORS(app, allow_headers='Content-Type')
    app.serverStatus = ServerStatus()
    # Allocate the backend
    # TODO is this a good way to determine what type of backend we should
    # instantiate? We should think carefully about this. The approach of
    # using the special strings __SIMULATED__ and __EMPTY__ seems OK for
    # now, but is certainly not ideal.
    dataSource = app.config["DATA_SOURCE"]
    if dataSource == "__SIMULATED__":
        randomSeed = app.config["SIMULATED_BACKEND_RANDOM_SEED"]
        numCalls = app.config["SIMULATED_BACKEND_NUM_CALLS"]
        variantDensity = app.config["SIMULATED_BACKEND_VARIANT_DENSITY"]
        numVariantSets = app.config["SIMULATED_BACKEND_NUM_VARIANT_SETS"]
        numReferenceSets = app.config[
            "SIMULATED_BACKEND_NUM_REFERENCE_SETS"]
        numReferencesPerReferenceSet = app.config[
            "SIMULATED_BACKEND_NUM_REFERENCES_PER_REFERENCE_SET"]
        numAlignments = app.config[
            "SIMULATED_BACKEND_NUM_ALIGNMENTS_PER_READ_GROUP"]
        theBackend = backend.SimulatedBackend(
            randomSeed, numCalls, variantDensity, numVariantSets,
            numReferenceSets, numReferencesPerReferenceSet, numAlignments)
    elif dataSource == "__EMPTY__":
        theBackend = backend.EmptyBackend()
    else:
        theBackend = backend.FileSystemBackend(dataSource)
    theBackend.setRequestValidation(app.config["REQUEST_VALIDATION"])
    theBackend.setResponseValidation(app.config["RESPONSE_VALIDATION"])
    theBackend.setDefaultPageSize(app.config["DEFAULT_PAGE_SIZE"])
    theBackend.setMaxResponseLength(app.config["MAX_RESPONSE_LENGTH"])
    app.backend = theBackend
    app.secret_key = os.urandom(SECRET_KEY_LENGTH)
    app.oidcClient = None
    app.tokenMap = None
    app.myPort = port
    if "OIDC_PROVIDER" in app.config:
        # The oic client. If we're testing, we don't want to verify
        # SSL certificates
        app.oidcClient = oic.oic.Client(
            verify_ssl=('TESTING' not in app.config))
        app.tokenMap = {}
        try:
            app.oidcClient.provider_config(app.config['OIDC_PROVIDER'])
        except requests.exceptions.ConnectionError:
            configResponse = message.ProviderConfigurationResponse(
                issuer=app.config['OIDC_PROVIDER'],
                authorization_endpoint=app.config['OIDC_AUTHZ_ENDPOINT'],
                token_endpoint=app.config['OIDC_TOKEN_ENDPOINT'],
                revocation_endpoint=app.config['OIDC_TOKEN_REV_ENDPOINT'])
            app.oidcClient.handle_provider_config(configResponse,
                                                  app.config['OIDC_PROVIDER'])

        # The redirect URI comes from the configuration.
        # If we are testing, then we allow the automatic creation of a
        # redirect uri if none is configured
        redirectUri = app.config.get('OIDC_REDIRECT_URI')
        if redirectUri is None and 'TESTING' in app.config:
            redirectUri = 'https://{0}:{1}/oauth2callback'.format(
                socket.gethostname(), app.myPort)
        app.oidcClient.redirect_uris = [redirectUri]
        if redirectUri is []:
            raise exceptions.ConfigurationException(
                'OIDC configuration requires a redirect uri')

        # We only support dynamic registration while testing.
        if ('registration_endpoint' in app.oidcClient.provider_info and
           'TESTING' in app.config):
            app.oidcClient.register(
                app.oidcClient.provider_info["registration_endpoint"],
                redirect_uris=[redirectUri])
        else:
            response = message.RegistrationResponse(
                client_id=app.config['OIDC_CLIENT_ID'],
                client_secret=app.config['OIDC_CLIENT_SECRET'],
                redirect_uris=[redirectUri],
                verify_ssl=False)
            app.oidcClient.store_registration_info(response)
        app.permissions = app.config['PERMISSIONS']


def getFlaskResponse(responseString, httpStatus=200):
    """
    Returns a Flask response object for the specified data and HTTP status.
    """
    return flask.Response(responseString, status=httpStatus, mimetype=MIMETYPE)


def handleHttpPost(request, endpoint):
    """
    Handles the specified HTTP POST request, which maps to the specified
    protocol handler endpoint and protocol request class.
    """
    if request.mimetype != MIMETYPE:
        raise exceptions.UnsupportedMediaTypeException()
    responseStr = endpoint(request.get_data())
    return getFlaskResponse(responseStr)


def handleList(id_, endpoint, request):
    """
    Handles the specified HTTP GET request, mapping to a list request
    """
    responseStr = endpoint(id_, request.args)
    return getFlaskResponse(responseStr)


def handleHttpGet(id_, endpoint):
    """
    Handles the specified HTTP GET request, which maps to the specified
    protocol handler endpoint and protocol request class
    """
    responseStr = endpoint(id_)
    return getFlaskResponse(responseStr)


def handleHttpOptions():
    """
    Handles the specified HTTP OPTIONS request.
    """
    response = getFlaskResponse("")
    response.headers.add("Access-Control-Request-Methods", "GET,POST,OPTIONS")
    return response


@app.errorhandler(Exception)
def handleException(exception):
    """
    Handles an exception that occurs somewhere in the process of handling
    a request.
    """
    if app.config['DEBUG']:
        app.log_exception(exception)
    serverException = exception
    if not isinstance(exception, exceptions.BaseServerException):
        serverException = exceptions.getServerError(exception)
    responseStr = serverException.toProtocolElement().toJsonString()
    return getFlaskResponse(responseStr, serverException.httpStatus)


def assertCorrectVersion(version):
    if not Version.isCurrentVersion(version):
        raise exceptions.VersionNotSupportedException()


def startLogin():
    """
    If we are not logged in, this generates the redirect URL to the OIDC
    provider and returns the redirect response
    :return: A redirect response to the OIDC provider
    """
    flask.session["state"] = oic.oauth2.rndstr(SECRET_KEY_LENGTH)
    flask.session["nonce"] = oic.oauth2.rndstr(SECRET_KEY_LENGTH)
    args = {
        "client_id": app.oidcClient.client_id,
        "response_type": "code",
        "scope": ["openid", "profile", "email"],
        "nonce": flask.session["nonce"],
        "redirect_uri": app.oidcClient.redirect_uris[0],
        "state": flask.session["state"]
    }

    result = app.oidcClient.do_authorization_request(
        request_args=args, state=flask.session["state"])
    return flask.redirect(result.url)


@app.before_request
def checkAuthentication():
    """
    The request will have a parameter 'key' if it came from the command line
    client, or have a session key of 'key' if it's the browser.
    If the token is not found, start the login process.
    If we are requesting particular datasets, then check that the user
    has access to those datasets. If not, raise an exception.

    If there is no oidcClient, we are running naked and we don't check.
    If we're being redirected to the oidcCallback we don't check.

    :returns None if all is ok (and the request handler continues as usual).
    Otherwise if the key was in the session (therefore we're in a browser)
    then startLogin() will redirect to the OIDC provider. If the key was in
    the request arguments, we're using the command line and just raise an
    exception.
    """
    if app.oidcClient is None:
        return
    if flask.request.endpoint == 'oidcCallback':
        return
    key = flask.session.get('key') or flask.request.args.get('key')
    if app.tokenMap.get(key) is None:
        if 'key' in flask.request.args:
            raise exceptions.NotAuthenticatedException()
        else:
            return startLogin()

    jsonBody = flask.request.get_json() or {}
    if "datasetIds" in jsonBody:
        userId = app.tokenMap[key]['userId']
        if userId not in app.permissions:
            raise exceptions.NotAuthenticatedException(userId)
        userDatasets = app.permissions[userId]
        validIds = [dataset for dataset in jsonBody['datasetIds']
                    if dataset in userDatasets]
        if validIds is []:
            raise exceptions.NotAuthenticatedException()


def handleFlaskGetRequest(version, id_, flaskRequest, endpoint):
    """
    Handles the specified flask request for one of the GET URLs
    at the specified version.  Invokes the specified endpoint to
    generate a response.
    """
    assertCorrectVersion(version)
    if flaskRequest.method == "GET":
        return handleHttpGet(id_, endpoint)
    else:
        raise exceptions.MethodNotAllowedException()


def handleFlaskListRequest(version, id_, flaskRequest, endpoint):
    """
    Handles the specified flask list request for one of the GET URLs
    at the specified version.  Invokes the specified endpoint to
    generate a response.
    """
    assertCorrectVersion(version)
    if flaskRequest.method == "GET":
        return handleList(id_, endpoint, flaskRequest)
    else:
        raise exceptions.MethodNotAllowedException()


def handleFlaskPostRequest(version, flaskRequest, endpoint):
    """
    Handles the specified flask request for one of the POST URLS
    at the specified version. Invokes the specified endpoint to
    generate a response.
    """
    assertCorrectVersion(version)
    if flaskRequest.method == "POST":
        return handleHttpPost(flaskRequest, endpoint)
    elif flaskRequest.method == "OPTIONS":
        return handleHttpOptions()
    else:
        raise exceptions.MethodNotAllowedException()


class DisplayedRoute(object):
    """
    Registers that a route should be displayed on the html page
    """
    def __init__(
            self, path, postMethod=False, pathDisplay=None):
        self.path = path
        self.methods = None
        if postMethod:
            methodDisplay = 'POST'
            self.methods = SEARCH_ENDPOINT_METHODS
        else:
            methodDisplay = 'GET'
        if pathDisplay is None:
            pathDisplay = path
        pathDisplay = pathDisplay.replace(
            '<version>', protocol.version)
        app.urls.append((methodDisplay, pathDisplay))

    def __call__(self, func):
        if self.methods is None:
            app.add_url_rule(self.path, func.func_name, func)
        else:
            app.add_url_rule(
                self.path, func.func_name, func, methods=self.methods)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            return result
        return wrapper


@app.route('/')
def index():
    return flask.render_template('index.html', info=app.serverStatus)


@app.route('/<version>')
def indexRedirect(version):
    try:
        isCurrentVersion = Version.isCurrentVersion(version)
    except TypeError:  # malformed "version string"
        raise exceptions.PathNotFoundException()
    if isCurrentVersion:
        return index()
    else:
        raise exceptions.PathNotFoundException()


@DisplayedRoute('/<version>/references/<id>')
def getReference(version, id):
    return handleFlaskGetRequest(
        version, id, flask.request, app.backend.getReference)


@DisplayedRoute('/<version>/referencesets/<id>')
def getReferenceSet(version, id):
    return handleFlaskGetRequest(
        version, id, flask.request, app.backend.getReferenceSet)


@DisplayedRoute('/<version>/references/<id>/bases')
def listReferenceBases(version, id):
    return handleFlaskListRequest(
        version, id, flask.request, app.backend.listReferenceBases)


@DisplayedRoute('/<version>/callsets/search', postMethod=True)
def searchCallSets(version):
    return handleFlaskPostRequest(
        version, flask.request, app.backend.searchCallSets)


@DisplayedRoute('/<version>/readgroupsets/search', postMethod=True)
def searchReadGroupSets(version):
    return handleFlaskPostRequest(
        version, flask.request, app.backend.searchReadGroupSets)


@DisplayedRoute('/<version>/reads/search', postMethod=True)
def searchReads(version):
    return handleFlaskPostRequest(
        version, flask.request, app.backend.searchReads)


@DisplayedRoute('/<version>/referencesets/search', postMethod=True)
def searchReferenceSets(version):
    return handleFlaskPostRequest(
        version, flask.request, app.backend.searchReferenceSets)


@DisplayedRoute('/<version>/references/search', postMethod=True)
def searchReferences(version):
    return handleFlaskPostRequest(
        version, flask.request, app.backend.searchReferences)


@DisplayedRoute('/<version>/variantsets/search', postMethod=True)
def searchVariantSets(version):
    return handleFlaskPostRequest(
        version, flask.request, app.backend.searchVariantSets)


@DisplayedRoute('/<version>/variants/search', postMethod=True)
def searchVariants(version):
    return handleFlaskPostRequest(
        version, flask.request, app.backend.searchVariants)


@DisplayedRoute('/<version>/datasets/search', postMethod=True)
def searchDatasets(version):
    return handleFlaskPostRequest(
        version, flask.request, app.backend.searchDatasets)


@DisplayedRoute('/<version>/variantsets/<no(search):id>')
def getVariantSet(version, id):
    return handleFlaskGetRequest(
        version, id, flask.request, app.backend.getVariantSet)


# The below paths have not yet been implemented


@app.route('/<version>/callsets/<no(search):id>')
def getCallset(version, id):
    raise exceptions.NotImplementedException()


@app.route('/<version>/alleles/<no(search):id>')
def getAllele(version, id):
    raise exceptions.NotImplementedException()


@app.route('/<version>/variants/<no(search):id>')
def getVariant(version, id):
    raise exceptions.NotImplementedException()


@app.route('/<version>/variantsets/<vsid>/sequences/<sid>')
def getVariantSetSequence(version, vsid, sid):
    raise exceptions.NotImplementedException()


@app.route('/<version>/feature/<id>')
def getFeature(version, id):
    raise exceptions.NotImplementedException()


@app.route('/<version>/sequences/<id>/bases')
def getSequenceBases(version, id):
    raise exceptions.NotImplementedException()


@app.route('/<version>/mode/<mode>')
def getMode(version, mode):
    raise exceptions.NotImplementedException()


@app.route('/<version>/datasets/<no(search):id>')
def getDataset(version, id):
    raise exceptions.NotImplementedException()


@app.route('/<version>/readgroupsets/<no(search):id>')
def getReadGroupSet(version, id):
    raise exceptions.NotImplementedException()


@app.route('/<version>/readgroups/<id>')
def getReadGroup(version, id):
    raise exceptions.NotImplementedException()


@app.route(
    '/<version>/genotypephenotype/search',
    methods=SEARCH_ENDPOINT_METHODS)
def searchGenotypePephenotype(version):
    raise exceptions.NotImplementedException()


@app.route('/<version>/individuals/search', methods=SEARCH_ENDPOINT_METHODS)
def searchIndividuals(version):
    raise exceptions.NotImplementedException()


@app.route('/<version>/samples/search', methods=SEARCH_ENDPOINT_METHODS)
def searchSamples(version):
    raise exceptions.NotImplementedException()


@app.route('/<version>/experiments/search', methods=SEARCH_ENDPOINT_METHODS)
def searchExperiments(version):
    raise exceptions.NotImplementedException()


@app.route(
    '/<version>/individualgroups/search',
    methods=SEARCH_ENDPOINT_METHODS)
def searchIndividualGroups(version):
    raise exceptions.NotImplementedException()


@app.route('/<version>/analyses/search', methods=SEARCH_ENDPOINT_METHODS)
def searchAnalyses(version):
    raise exceptions.NotImplementedException()


@app.route('/<version>/sequences/search', methods=SEARCH_ENDPOINT_METHODS)
def searchSequences(version):
    raise exceptions.NotImplementedException()


@app.route('/<version>/joins/search', methods=SEARCH_ENDPOINT_METHODS)
def searchJoins(version):
    raise exceptions.NotImplementedException()


@app.route('/<version>/subgraph/segments', methods=SEARCH_ENDPOINT_METHODS)
def subgraphSegments(version):
    raise exceptions.NotImplementedException()


@app.route('/<version>/subgraph/joins', methods=SEARCH_ENDPOINT_METHODS)
def subgraphJoins(version):
    raise exceptions.NotImplementedException()


@app.route('/<version>/features/search', methods=SEARCH_ENDPOINT_METHODS)
def searchFeatures(version):
    raise exceptions.NotImplementedException()


@app.route(
    '/<version>/variantsets/<id>/sequences/search',
    methods=SEARCH_ENDPOINT_METHODS)
def searchVariantSetSequences(version, id):
    raise exceptions.NotImplementedException()


@app.route('/<version>/alleles/search', methods=SEARCH_ENDPOINT_METHODS)
def searchAlleles(version):
    raise exceptions.NotImplementedException()


@app.route('/oauth2callback', methods=['GET'])
def oidcCallback():
    """
    Once the authorization provider has cleared the user, the browser
    is returned here with a code. This function takes that code and
    checks it with the authorization provider to prove that it is valid,
    and get a bit more information about the user (which we don't use).

    A token is generated and given to the user, and the authorization info
    retrieved above is stored against this token. Later, when a client
    connects with this token, it is assumed to be a valid user.

    :return: A display of the authentication token to use in the client. If
    OIDC is not configured, raises a NotImplementedException.
    """
    if app.oidcClient is None:
        raise exceptions.NotImplementedException()
    response = dict(flask.request.args.iteritems(multi=True))
    aresp = app.oidcClient.parse_response(message.AuthorizationResponse,
                                          info=response,
                                          sformat='dict')
    sessState = flask.session.get('state')
    respState = aresp['state']
    if not isinstance(aresp,
                      message.AuthorizationResponse) or respState != sessState:
        raise exceptions.NotAuthenticatedException()

    args = {
        "code": aresp['code'],
        "redirect_uri": app.oidcClient.redirect_uris[0],
        "client_id": app.oidcClient.client_id,
        "client_secret": app.oidcClient.client_secret
    }
    atr = app.oidcClient.do_access_token_request(
        scope="openid, email",
        state=respState,
        request_args=args)

    if not isinstance(atr, message.AccessTokenResponse):
        raise exceptions.NotAuthenticatedException()

    atrDict = atr.to_dict()
    if flask.session.get('nonce') != atrDict['id_token']['nonce']:
        raise exceptions.NotAuthenticatedException()

    userInfo = app.oidcClient.do_user_info_request(state=aresp["state"])
    key = oic.oauth2.rndstr(SECRET_KEY_LENGTH)
    flask.session['key'] = key
    userId = userInfo['email']
    flask.session['userId'] = userId
    app.tokenMap[key] = {'code': aresp["code"],
                         'state': respState,
                         'accessTokenResponse': atrDict,
                         'userId': userId,
                         'userInfo': userInfo}
    # flask.url_for is broken. It relies on SERVER_NAME for both name
    # and port, and defaults to 'localhost' if not found. Therefore
    # we need to fix the returned url
    indexUrl = flask.url_for('index', _external=True)
    indexParts = list(urlparse.urlparse(indexUrl))
    if ':' not in indexParts[1]:
        indexParts[1] = '{0}:{1}'.format(socket.gethostname(), app.myPort)
        indexUrl = urlparse.urlunparse(indexParts)
    response = flask.redirect(indexUrl)
    return response


# The below methods ensure that JSON is returned for various errors
# instead of the default, html


@app.errorhandler(404)
def pathNotFoundHandler(errorString):
    return handleException(exceptions.PathNotFoundException())


@app.errorhandler(405)
def methodNotAllowedHandler(errorString):
    return handleException(exceptions.MethodNotAllowedException())


@app.errorhandler(403)
def notAuthenticatedHandler(errorString):
    return handleException(exceptions.NotAuthenticatedException())
