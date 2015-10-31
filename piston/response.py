class HeadersResponse(object):

    fieldset = True

    def __init__(self, result, http_headers={}, code=200):
        self.result = result
        self.http_headers = http_headers
        self.status_code = code


class NoFieldsetResponse(HeadersResponse):

    fieldset = False


class RestResponse(NoFieldsetResponse):

    def __init__(self, msg, http_headers={}, code=200):
        super(RestResponse, self).__init__(result={'messages': msg}, http_headers=http_headers, code=code)


class RestOkResponse(NoFieldsetResponse):

    def __init__(self, msg, http_headers={}, code=200):
        super(RestOkResponse, self).__init__(result={'messages': {'success': msg}}, http_headers=http_headers,
                                             code=code)


class RestCreatedResponse(HeadersResponse):

    def __init__(self, result, http_headers={}, code=201):
        super(RestCreatedResponse, self).__init__(result=result, http_headers=http_headers, code=code)


class RestNoContentResponse(NoFieldsetResponse):

    def __init__(self, http_headers={}, code=204):
        super(RestNoContentResponse, self).__init__(result='', http_headers=http_headers, code=code)


class RestErrorsResponse(HeadersResponse):

    def __init__(self, msg, http_headers={}, code=400):
        super(RestErrorsResponse, self).__init__(result={'messages': {'errors': msg}}, http_headers=http_headers,
                                                 code=code)


class RestErrorResponse(NoFieldsetResponse):

    def __init__(self, msg, http_headers={}, code=400):
        super(RestErrorResponse, self).__init__(result={'messages': {'error': msg}}, http_headers=http_headers,
                                                code=code)
