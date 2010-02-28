# -*- coding: utf-8 -
#
# This file is part of restkit released under the MIT license. 
# See the NOTICE for more information.



version_info = (1, 0, 0)
__version__ =  ".".join(map(str, version_info))

try:
    from libs.restkit.errors import ResourceNotFound, Unauthorized, RequestFailed,\
RedirectLimit, RequestError, InvalidUrl, ResponseError, ProxyError, ResourceError
    from libs.restkit.client import HttpConnection, HttpResponse
    from libs.restkit.resource import Resource
    from libs.restkit.pool import ConnectionPool
    from libs.restkit.filters import BasicAuth, SimpleProxy
    from libs.restkit.oauth2.filter import OAuthFilter
except ImportError:
    import traceback
    traceback.print_exc()
    
import urlparse
    
def request(url, method='GET', body=None, headers=None, pool_instance=None, 
        follow_redirect=False, filters=None, key_file=None, cert_file=None):
    """ Quick shortcut method to pass a request
    
    :param url: str, url string
    :param method: str, by default GET. http verbs
    :param body: the body, could be a string, an iterator or a file-like object
    :param headers: dict or list of tupple, http headers
    :pool intance: instance inherited from `restkit.pool.PoolInterface`. 
    It allows you to share and reuse connections connections.
    :param follow_redirect: boolean, by default is false. If true, 
    if the HTTP status is 301, 302 or 303 the client will follow
    the location.
    :param filters: list, list of http filters. see the doc of http filters 
    for more info
    :param key_file: the key fle to use with ssl
    :param cert_file: the cert file to use with ssl
    
    """
    # detect credentials from url
    u = urlparse.urlparse(url)
    if u.username is not None:
        password = u.password or ""
        filters = filters or []
        url = urlparse.urlunparse((u.scheme, u.netloc.split("@")[-1],
            u.path, u.params, u.query, u.fragment))
        filters.append(BasicAuth(u.username, password))
    
    http_client = HttpConnection(follow_redirect=follow_redirect,
            filters=filters, key_file=key_file, cert_file=cert_file,
            pool_instance=pool_instance)
    return http_client.request(url, method=method, body=body, 
        headers=headers)