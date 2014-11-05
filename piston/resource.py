from __future__ import unicode_literals

import re
import warnings

from django.conf import settings
from django.http import HttpResponse
from django.utils.decorators import classonlymethod
from django.utils.encoding import force_text
from django.db.models.base import Model
from django.http.response import Http404
from django.db import transaction
from django.forms.models import modelform_factory
from django.core.exceptions import ObjectDoesNotExist

from functools import update_wrapper

from .paginator import Paginator
from .response import (HeadersResponse, RestErrorResponse, RestErrorsResponse, RestCreatedResponse,
                       RestNoConetentResponse)
from .exception import (RestException, ConflictException, NotAllowedException, DataInvalidException,
                        ResourceNotFoundException, NotAllowedMethodException, DuplicateEntryException)
from .forms import RestModelForm
from .utils import get_object_or_none, list_to_dict, dict_to_list, flat_list, rc, set_rest_context_to_request
from .serializer import ResourceSerializer
from .exception import UnsupportedMediaTypeException, MimerDataException
from piston.utils import RFS, rfs

typemapper = { }
resource_tracker = [ ]


class ResourceMetaClass(type):
    """
    Metaclass that keeps a registry of class -> resource
    mappings.
    """
    def __new__(cls, name, bases, attrs):
        new_cls = type.__new__(cls, name, bases, attrs)
        if new_cls.register:
            def already_registered(model):
                return typemapper.get(model)

            if hasattr(new_cls, 'model'):
                if already_registered(new_cls.model):
                    if not getattr(settings, 'PISTON_IGNORE_DUPE_MODELS', False):
                        warnings.warn("Resource already registered for model %s, "
                            "you may experience inconsistent results." % new_cls.model.__name__)

                typemapper[new_cls.model] = new_cls

            if name != 'BaseResource':
                resource_tracker.append(new_cls)

        return new_cls


class PermissionsResourceMixin(object):

    allowed_methods = ('get', 'post', 'put', 'delete', 'head', 'options')

    def _get_via(self, via=None):
        via = via or []
        via = list(via)
        via.append(self)
        return via

    def get_allowed_methods(self, obj=None, restricted_methods=None):
        allowed_methods = []

        tested_methods = set(self.allowed_methods)
        if restricted_methods is not None:
            tested_methods = tested_methods.intersection(restricted_methods)

        for method in tested_methods:
            try:
                self._check_permission(method, obj=obj)
                allowed_methods.append(method)
            except (NotImplementedError, NotAllowedException):
                pass
        return allowed_methods

    def __getattr__(self, name):
        m = re.match(r'_check_(\w+)_permission', name)
        if m:
            def _call_check(*args, **kwargs):
                return self._check_permission(m.group(1), *args, **kwargs)
            return _call_check

        raise AttributeError("%r object has no attribute %r" % (self.__class__, name))

    def _check_permission(self, name, *args, **kwargs):
        if not hasattr(self, 'has_%s_permission' % name):
            raise NotImplementedError('Please implement method has_%s_permission to %s' % (name, self.__class__))

        if not getattr(self, 'has_%s_permission' % name)(*args, **kwargs):
            raise NotAllowedException

    def has_get_permission(self, obj=None, via=None):
        return 'get' in self.allowed_methods

    def has_post_permission(self, obj=None, via=None):
        return 'post' in self.allowed_methods

    def has_put_permission(self, obj=None, via=None):
        return 'put' in self.allowed_methods

    def has_delete_permission(self, obj=None, via=None):
        return 'delete' in self.allowed_methods

    def has_head_permission(self, obj=None, via=None):
        return 'head' in self.allowed_methods and self.has_get_permission(obj, via)

    def has_options_permission(self, obj=None, via=None):
        return 'options' in self.allowed_methods


class BaseResource(PermissionsResourceMixin):
    """
    BaseResource that gives you CRUD for free.
    You are supposed to subclass this for specific
    functionality.

    All CRUD methods (`read`/`update`/`create`/`delete`)
    receive a request as the first argument from the
    resource. Use this for checking `request.user`, etc.
    """
    __metaclass__ = ResourceMetaClass

    allowed_methods = ('get', 'post', 'put', 'delete', 'head', 'options')
    serializer = ResourceSerializer
    register = False
    csrf_exempt = True
    cache = None

    DEFAULT_REST_CONTEXT_MAPPING = {
        'serialization_format': ('HTTP_X_SERIALIZATION_FORMAT', '_serialization_format'),
        'fields': ('HTTP_X_FIELDS', '_fields'),
        'offset': ('HTTP_X_OFFSET', '_offset'),
        'base': ('HTTP_X_BASE', '_base'),
        'accept': ('HTTP_ACCEPT', '_accept'),
        'content_type': ('CONTENT_TYPE', '_content_type'),
    }

    def __init__(self, request):
        self.request = request
        self.args = []
        self.kwargs = {}

    def _get_serialization_format(self):
        serialization_format = self.request._rest_context.get('serialization_format',
                                                              self.serializer.SERIALIZATION_TYPES.RAW)
        if serialization_format not in self.serializer.SERIALIZATION_TYPES:
            return self.serializer.SERIALIZATION_TYPES.RAW
        return serialization_format

    def get_fields(self, obj=None):
        return None

    def get_default_detailed_fields(self, obj=None):
        return self.get_fields(obj)

    def get_default_general_fields(self, obj=None):
        return self.get_fields(obj)

    def __getattr__(self, name):
        if name == 'head':
            return self.get
        else:
            return super(BaseResource, self).__getattr__(name)

    def _get_obj_or_none(self, pk=None):
        """
        Should return one object
        """
        return None

    def _get_obj_or_404(self, pk=None):
        obj = self._get_obj_or_none(pk)
        if not obj:
            raise Http404
        return obj

    def options(self):
        obj = self._get_obj_or_none()
        allowed_methods = [method.upper() for method in self.get_allowed_methods(obj)]
        return HeadersResponse(None, http_headers={'Allowed':','.join(allowed_methods)})

    def _is_single_obj_request(self, result):
        return isinstance(result, dict)

    def _get_requested_fieldset(self, result):
        return RFS.create_from_string(self.request._rest_context.get('fields', ''))

    def _serialize(self, result):
        return self.serializer(self).serialize(
            self.request, result, self._get_requested_fieldset(result),
            self._get_serialization_format()
        )

    def _deserialize(self):
        return self.serializer(self).deserialize(self.request)

    def _get_error_response(self, exception):

        responses = {
            MimerDataException: rc.BAD_REQUEST,
            NotAllowedException: rc.FORBIDDEN,
            UnsupportedMediaTypeException: rc.UNSUPPORTED_MEDIA_TYPE,
            Http404: rc.NOT_FOUND,
            ResourceNotFoundException: rc.NOT_FOUND,
            NotAllowedMethodException: rc.METHOD_NOT_ALLOWED,
            DuplicateEntryException: rc.DUPLICATE_ENTRY,
            ConflictException: rc.DUPLICATE_ENTRY,
        }
        return responses.get(type(exception))

    def _get_response_data(self):
        status_code = 200
        http_headers = {}
        try:
            self.request = self._deserialize()

            rm = self.request.method.lower()
            meth = getattr(self, rm, None)
            if not meth or rm not in self.allowed_methods:
                result = self._get_error_response(NotAllowedMethodException())
            else:
                self._check_permission(rm)
                result = meth()
        except (MimerDataException, NotAllowedException, UnsupportedMediaTypeException, Http404) as ex:
            result = self._get_error_response(ex)
        if isinstance(result, HeadersResponse):
            http_headers = result.http_headers
            status_code = result.status_code
            result = result.result

        if isinstance(result, HttpResponse):
            status_code = result.status_code
            result = result._container
        return result, http_headers, status_code

    def _set_response_headers(self, response, result, http_headers):
        for header, value in self._get_headers(result, http_headers).items():
            response[header] = value

    def _get_response(self):
        result, http_headers, status_code = self._get_response_data()
        try:
            content, ct = self._serialize(result)
        except UnsupportedMediaTypeException:
            content = ''
            status_code = 415
            ct = getattr(settings, 'PISTON_DEFAULT_CONVERTER', 'json')

        if result is None:
            content = ''

        response = content
        if not isinstance(content, HttpResponse):
            response = HttpResponse(content, content_type=ct, status=status_code)

        self._set_response_headers(response, result, http_headers)
        return response

    def _get_from_cache(self):
        if self.cache:
            return self.cache.get_response(self.request)

    def _store_to_cache(self, response):
        if self.cache and response.status_code < 400:
            self.cache.cache_response(self.request, response)

    def _get_headers_queryset_context_mapping(self):
        return self.DEFAULT_REST_CONTEXT_MAPPING.copy()

    def _get_context(self):
        context = {}
        for key, (header_key, queryset_key) in self._get_headers_queryset_context_mapping().items():
            val = self.request.GET.get(queryset_key, self.request.META.get(header_key))
            if val:
                context[key] = val
        return context

    def dispatch(self, request, *args, **kwargs):
        set_rest_context_to_request(request, self._get_headers_queryset_context_mapping())
        response = self._get_from_cache()
        if response:
            return response
        response = self._get_response()
        self._store_to_cache(response)
        return response

    def _get_headers(self, result, http_headers):
        http_headers['X-Serialization-Format-Options'] = ','.join(self.serializer.SERIALIZATION_TYPES)
        http_headers['Cache-Control'] = 'private, no-cache, no-store, max-age=0'
        http_headers['Pragma'] = 'no-cache'
        http_headers['Expires'] = '0'
        fields = self.get_fields(obj=result)
        if fields:
            http_headers['X-Fields-Options'] = ','.join(fields.flat())
        return http_headers

    @classonlymethod
    def as_view(cls, allowed_methods=None, **initkwargs):
        def view(request, *args, **kwargs):
            self = cls(request, **initkwargs)
            self.request = request
            self.args = args
            self.kwargs = kwargs
            if allowed_methods is not None:
                self.allowed_methods = set(allowed_methods) & set(cls.allowed_methods)
            else:
                self.allowed_methods = set(cls.allowed_methods)
            return self.dispatch(request, *args, **kwargs)
        view.csrf_exempt = cls.csrf_exempt

        # take name and docstring from class
        update_wrapper(view, cls, updated=())

        # and possible attributes set by decorators
        # like csrf_exempt from dispatch
        update_wrapper(view, cls.dispatch, assigned=())
        return view


class DefaultRestObjectResource(PermissionsResourceMixin):

    default_detailed_fields = ('id', '_obj_name')
    default_general_fields = ('id', '_obj_name')
    extra_fields = ()
    guest_fields = ('id', '_obj_name')
    allowed_methods = ()

    def _obj_name(self, obj):
        return force_text(obj)

    def get_fields(self, obj=None):
        return self.get_default_detailed_fields(obj).join(
            self.get_default_detailed_fields(obj)).join(self.get_extra_fields(obj))

    def get_default_detailed_fields(self, obj=None):
        return rfs(self.default_detailed_fields)

    def get_default_general_fields(self, obj=None):
        return rfs(self.default_general_fields)

    def get_extra_fields(self, obj=None):
        return rfs(self.extra_fields)

    def get_guest_fields(self, request):
        return rfs(self.guest_fields)


class BaseObjectResource(DefaultRestObjectResource, BaseResource):

    allowed_methods = ('get', 'post', 'put', 'delete', 'head', 'options')
    pk_name = 'pk'
    pk_field_name = 'id'

    def _flatten_dict(self, dct):
        if isinstance(dct, dict):
            return dict([ (str(k), dct.get(k)) for k in dct.keys() ])
        return {}

    def _get_queryset(self):
        """
        Should return list or db queryset
        """
        raise NotImplementedError

    def _get_obj_or_none(self, pk=None):
        """
        Should return one object
        """
        raise NotImplementedError

    def _filter_queryset(self, qs):
        """
        Should contain implementation for objects filtering
        """
        return qs

    def _order_queryset(self, qs):
        """
        Should contain implementation for objects ordering
        """
        return qs

    def _exists_obj(self, **kwargs):
        """
        Should return true if object exists
        """
        raise NotImplementedError

    def post(self):
        pk = self.kwargs.get(self.pk_name)
        data = self._flatten_dict(self.request.data)
        if pk and self._exists_obj(pk=pk):
            raise DuplicateEntryException
        try:
            inst = self._atomic_create_or_update(data)
        except DataInvalidException as ex:
            return RestErrorsResponse(ex.errors)
        except NotAllowedException:
            raise
        except RestException as ex:
            return RestErrorResponse(ex.message)
        return RestCreatedResponse(inst)

    def get(self):
        pk = self.kwargs.get(self.pk_name)
        if pk:
            return self._get_obj_or_404()
        try:
            qs = self._filter_queryset(self._get_queryset().all())
            qs = self._order_queryset(qs)
            paginator = Paginator(qs, self.request)
            return HeadersResponse(paginator.page_qs, {'X-Total': paginator.total})
        except RestException as ex:
            return RestErrorResponse(ex.message)
        except Http404:
            raise
        except Exception as ex:
            return HeadersResponse([], {'X-Total': 0})

    def put(self):
        pk = self.kwargs.get(self.pk_name)
        data = self._flatten_dict(self.request.data)
        data[self.pk_field_name] = pk
        try:
            return self._atomic_create_or_update(data)
        except DataInvalidException as ex:
            return RestErrorsResponse(ex.errors)
        except (ConflictException, NotAllowedException):
            raise
        except RestException as ex:
            return RestErrorResponse(ex.message)

    def delete(self):
        pk = self.kwargs.get(self.pk_name)
        self._delete(pk)
        return RestNoConetentResponse()

    def _delete(self, pk, via=None):
        via = via or []
        obj = self._get_obj_or_404(pk)
        self._check_delete_permission(obj, via)
        self._pre_delete_obj(obj)
        self._delete_obj(obj)
        self._post_delete_obj(obj)

    def _pre_delete_obj(self, obj):
        pass

    def _delete_obj(self, obj):
        raise NotImplementedError

    def _post_delete_obj(self, obj):
        pass

    @transaction.atomic
    def _atomic_create_or_update(self, data):
        """
        Atomic object creation
        """
        return self._create_or_update(data)

    def _get_instance(self, data):
        """
        Should contains implementation for get object according to input data values
        """
        raise NotImplementedError

    def _generate_form_class(self, inst, exclude=[]):
        return self.form_class

    def _get_form(self, fields=None, inst=None, data=None, files=None, initial={}):
        # When is send PUT (resource instance exists), it is possible send only changed values.
        exclude = []

        kwargs = {}
        if inst:
            kwargs['instance'] = inst
        if data is not None:
            kwargs['data'] = data
            kwargs['files'] = files

        form_class = self._generate_form_class(inst, exclude)
        return form_class(initial=initial, **kwargs)

    def _get_form_initial(self, obj):
        return {}

    def _can_save_obj(self, change, obj, form, via):
        if change and (not via or form.has_changed()):
            self._check_put_permission(obj, via)
        elif not change:
            self._check_post_permission(obj, via)

        return not change or self.has_put_permission(obj, via=via)

    def _create_or_update(self, data, via=None):
        """
        Helper for creating or updating resource
        """
        from piston.data_processor import data_preprocessors
        from piston.data_processor import data_postprocessors

        if via is None:
            via = []

        inst = self._get_instance(data)
        change = inst and True or False

        files = self.request.FILES

        form = self._get_form(inst=inst, data=data, initial=self._get_form_initial(inst))

        for preprocessor in data_preprocessors.get_processors(type(self)):
            data, files = preprocessor(self, form, inst, via).process_data(data, files)

        form = self._get_form(fields=form.fields.keys(), inst=inst, data=data, files=files,
                             initial=self._get_form_initial(inst))

        errors = form.is_invalid()
        if errors:
            raise DataInvalidException(errors)

        inst = form.save(commit=False)

        can_save_obj = self._can_save_obj(change, inst, form, via)
        if can_save_obj:
            self._pre_save_obj(inst, form, change)
            self._save_obj(inst, form, change)
            if hasattr(form, 'save_m2m'):
                form.save_m2m()

        if inst.pk:
            for preprocessor in data_postprocessors.get_processors(type(self)):
                data, files = preprocessor(self, form, inst, via).process_data(data, files)

        if can_save_obj:
            self._post_save_obj(inst, form, change)
        return inst

    def _pre_save_obj(self, obj, form, change):
        pass

    def _save_obj(self, obj, form, change):
        raise NotImplementedError

    def _post_save_obj(self, obj, form, change):
        pass


class BaseModelResource(BaseObjectResource):

    register = True
    form_class = RestModelForm

    def _get_queryset(self):
        return self.model.objects.all()

    def _get_obj_or_none(self, pk=None):
        return get_object_or_none(self._get_queryset(), pk=(pk or self.kwargs.get(self.pk_name)))

    def _exists_obj(self, **kwargs):
        return self.model.objects.filter(**kwargs).exists()

    def _is_single_obj_request(self, result):
        return isinstance(result, Model)

    def _delete_obj(self, obj):
        obj.delete()

    def _save_obj(self, obj, form, change):
        obj.save()

    def _get_exclude(self, obj=None):
        return []

    def _get_form_class(self, inst):
        return self.form_class

    def _get_instance(self, data):
        # If data contains id this method is update otherwise create
        inst = None
        pk = data.get(self.pk_field_name)
        if pk:
            try:
                try:
                    inst = self._get_queryset().get(pk=pk)
                except (ObjectDoesNotExist, ValueError):
                    if self.model.objects.filter(pk=pk).exists():
                        raise ConflictException
            except ValueError:
                pass
        return inst

    def _get_form_fields(self, obj=None):
        return None

    def _generate_form_class(self, inst, exclude=[]):
        exclude = list(self._get_exclude(inst)) + exclude
        form_class = self._get_form_class(inst)
        fields = self._get_form_fields(inst)

        if hasattr(form_class, '_meta') and form_class._meta.exclude:
            exclude.extend(form_class._meta.exclude)
        return modelform_factory(self.model, form=form_class, exclude=exclude, fields=fields)
