import sys
import copy
import logging
from StringIO import StringIO

from django.conf import settings
from django.core import serializers
from django.core.exceptions import ObjectDoesNotExist, MultipleObjectsReturned,\
    FieldError
from django.db import models
from django.db.models import signals
from django.db.models.options import Options
from django.db.models.loading import register_models, get_model
from django.db.models.base import ModelBase, subclass_exception, \
    get_absolute_url, method_get_order, method_set_order
from django.db.models.fields.related import (OneToOneField, add_lazy_relation)
from django.utils.functional import curry
from functools import update_wrapper

from django.utils.encoding import force_unicode, smart_unicode

from restkit import Resource, RequestFailed, ResourceNotFound
from django_roa.db.exceptions import ROAException

logger = logging.getLogger("django_roa")

ROA_HEADERS = getattr(settings, 'ROA_HEADERS', {})
ROA_FORMAT = getattr(settings, 'ROA_FORMAT', 'json')
ROA_FILTERS = getattr(settings, 'ROA_FILTERS', {})
ROA_MODEL_NAME_MAPPING = getattr(settings, 'ROA_MODEL_NAME_MAPPING', [])
ROA_MODEL_CREATE_MAPPING = getattr(settings, 'ROA_MODEL_CREATE_MAPPING', {})
ROA_MODEL_UPDATE_MAPPING = getattr(settings, 'ROA_MODEL_UPDATE_MAPPING', {})
ROA_CUSTOM_ARGS = getattr(settings, "ROA_CUSTOM_ARGS", {})

DEFAULT_CHARSET = getattr(settings, 'DEFAULT_CHARSET', 'utf-8')


class ROAModelBase(ModelBase):
    def __new__(cls, name, bases, attrs):
        """
        Exactly the same except the line with ``isinstance(b, ROAModelBase)``.
        """
        super_new = super(ModelBase, cls).__new__

        # six.with_metaclass() inserts an extra class called 'NewBase' in the
        # inheritance tree: Model -> NewBase -> object. But the initialization
        # should be executed only once for a given model class.

        # attrs will never be empty for classes declared in the standard way
        # (ie. with the `class` keyword). This is quite robust.
        if name == 'NewBase' and attrs == {}:
            return super_new(cls, name, bases, attrs)

        # Also ensure initialization is only performed for subclasses of Model
        # (excluding Model class itself).
        parents = [b for b in bases if isinstance(b, ROAModelBase) and
                not (b.__name__ == 'NewBase' and b.__mro__ == (b, object))]
        if not parents:
            return super_new(cls, name, bases, attrs)

        # Create the class.
        module = attrs.pop('__module__')
        new_class = super_new(cls, name, bases, {'__module__': module})
        attr_meta = attrs.pop('Meta', None)
        abstract = getattr(attr_meta, 'abstract', False)
        if not attr_meta:
            meta = getattr(new_class, 'Meta', None)
        else:
            meta = attr_meta
        base_meta = getattr(new_class, '_meta', None)

        if getattr(meta, 'app_label', None) is None:
            # Figure out the app_label by looking one level up.
            # For 'django.contrib.sites.models', this would be 'sites'.
            model_module = sys.modules[new_class.__module__]
            kwargs = {"app_label": model_module.__name__.split('.')[-2]}
        else:
            kwargs = {}

        new_class.add_to_class('_meta', Options(meta, **kwargs))
        if not abstract:
            new_class.add_to_class('DoesNotExist', subclass_exception(str('DoesNotExist'),
                    tuple(x.DoesNotExist
                          for x in parents if hasattr(x, '_meta') and not x._meta.abstract)
                    or (ObjectDoesNotExist,),
                    module, attached_to=new_class))
            new_class.add_to_class('MultipleObjectsReturned', subclass_exception(str('MultipleObjectsReturned'),
                    tuple(x.MultipleObjectsReturned
                          for x in parents if hasattr(x, '_meta') and not x._meta.abstract)
                    or (MultipleObjectsReturned,),
                    module, attached_to=new_class))
            if base_meta and not base_meta.abstract:
                # Non-abstract child classes inherit some attributes from their
                # non-abstract parent (unless an ABC comes before it in the
                # method resolution order).
                if not hasattr(meta, 'ordering'):
                    new_class._meta.ordering = base_meta.ordering
                if not hasattr(meta, 'get_latest_by'):
                    new_class._meta.get_latest_by = base_meta.get_latest_by

        is_proxy = new_class._meta.proxy

        # If the model is a proxy, ensure that the base class
        # hasn't been swapped out.
        if is_proxy and base_meta and base_meta.swapped:
            raise TypeError("%s cannot proxy the swapped model '%s'." % (name, base_meta.swapped))

        if getattr(new_class, '_default_manager', None):
            if not is_proxy:
                # Multi-table inheritance doesn't inherit default manager from
                # parents.
                new_class._default_manager = None
                new_class._base_manager = None
            else:
                # Proxy classes do inherit parent's default manager, if none is
                # set explicitly.
                new_class._default_manager = new_class._default_manager._copy_to_model(new_class)
                new_class._base_manager = new_class._base_manager._copy_to_model(new_class)

        # Bail out early if we have already created this class.
        m = get_model(new_class._meta.app_label, name,
                      seed_cache=False, only_installed=False)
        if m is not None:
            return m

        # Add all attributes to the class.
        for obj_name, obj in attrs.items():
            new_class.add_to_class(obj_name, obj)

        # All the fields of any type declared on this model
        new_fields = new_class._meta.local_fields + \
                     new_class._meta.local_many_to_many + \
                     new_class._meta.virtual_fields
        field_names = set([f.name for f in new_fields])

        # Basic setup for proxy models.
        if is_proxy:
            base = None
            for parent in [cls for cls in parents if hasattr(cls, '_meta')]:
                if parent._meta.abstract:
                    if parent._meta.fields:
                        raise TypeError("Abstract base class containing model fields not permitted for proxy model '%s'." % name)
                    else:
                        continue
                if base is not None:
                    raise TypeError("Proxy model '%s' has more than one non-abstract model base class." % name)
                else:
                    base = parent
            if base is None:
                raise TypeError("Proxy model '%s' has no non-abstract model base class." % name)
            if (new_class._meta.local_fields or
                    new_class._meta.local_many_to_many):
                raise FieldError("Proxy model '%s' contains model fields." % name)
            new_class._meta.setup_proxy(base)
            new_class._meta.concrete_model = base._meta.concrete_model
        else:
            new_class._meta.concrete_model = new_class

        # Do the appropriate setup for any model parents.
        o2o_map = dict([(f.rel.to, f) for f in new_class._meta.local_fields
                if isinstance(f, OneToOneField)])

        for base in parents:
            original_base = base
            if not hasattr(base, '_meta'):
                # Things without _meta aren't functional models, so they're
                # uninteresting parents.
                continue

            parent_fields = base._meta.local_fields + base._meta.local_many_to_many
            # Check for clashes between locally declared fields and those
            # on the base classes (we cannot handle shadowed fields at the
            # moment).
            for field in parent_fields:
                if field.name in field_names:
                    raise FieldError('Local field %r in class %r clashes '
                                     'with field of similar name from '
                                     'base class %r' %
                                        (field.name, name, base.__name__))
            if not base._meta.abstract:
                # Concrete classes...
                base = base._meta.concrete_model
                if base in o2o_map:
                    field = o2o_map[base]
                elif not is_proxy:
                    attr_name = '%s_ptr' % base._meta.model_name
                    field = OneToOneField(base, name=attr_name,
                            auto_created=True, parent_link=True)
                    new_class.add_to_class(attr_name, field)
                else:
                    field = None
                new_class._meta.parents[base] = field
            else:
                # .. and abstract ones.
                for field in parent_fields:
                    new_class.add_to_class(field.name, copy.deepcopy(field))

                # Pass any non-abstract parent classes onto child.
                new_class._meta.parents.update(base._meta.parents)

            # Inherit managers from the abstract base classes.
            new_class.copy_managers(base._meta.abstract_managers)

            # Proxy models inherit the non-abstract managers from their base,
            # unless they have redefined any of them.
            if is_proxy:
                new_class.copy_managers(original_base._meta.concrete_managers)

            # Inherit virtual fields (like GenericForeignKey) from the parent
            # class
            for field in base._meta.virtual_fields:
                if base._meta.abstract and field.name in field_names:
                    raise FieldError('Local field %r in class %r clashes '\
                                     'with field of similar name from '\
                                     'abstract base class %r' % \
                                        (field.name, name, base.__name__))
                new_class.add_to_class(field.name, copy.deepcopy(field))

        if abstract:
            # Abstract base models can't be instantiated and don't appear in
            # the list of models for an app. We do the final setup for them a
            # little differently from normal models.
            attr_meta.abstract = False
            new_class.Meta = attr_meta
            return new_class

        new_class._prepare()
        register_models(new_class._meta.app_label, new_class)

        # Because of the way imports happen (recursively), we may or may not be
        # the first time this model tries to register with the framework. There
        # should only be one class for each model, so we always return the
        # registered version.
        return get_model(new_class._meta.app_label, name,
                         seed_cache=False, only_installed=False)

    def _prepare(cls):
        """
        Creates some methods once self._meta has been populated.
        """
        opts = cls._meta
        opts._prepare(cls)

        if opts.order_with_respect_to:
            cls.get_next_in_order = curry(cls._get_next_or_previous_in_order, is_next=True)
            cls.get_previous_in_order = curry(cls._get_next_or_previous_in_order, is_next=False)

            # defer creating accessors on the foreign class until we are
            # certain it has been created
            def make_foreign_order_accessors(field, model, cls):
                setattr(
                    field.rel.to,
                    'get_%s_order' % cls.__name__.lower(),
                    curry(method_get_order, cls)
                )
                setattr(
                    field.rel.to,
                    'set_%s_order' % cls.__name__.lower(),
                    curry(method_set_order, cls)
                )
            add_lazy_relation(
                cls,
                opts.order_with_respect_to,
                opts.order_with_respect_to.rel.to,
                make_foreign_order_accessors
            )

        # Give the class a docstring -- its definition.
        if cls.__doc__ is None:
            cls.__doc__ = "%s(%s)" % (cls.__name__, ", ".join([f.attname for f in opts.fields]))

        if hasattr(cls, 'get_absolute_url'):
            cls.get_absolute_url = update_wrapper(curry(get_absolute_url, opts, cls.get_absolute_url),
                                                  cls.get_absolute_url)

        if hasattr(cls, 'get_resource_url_list'):
            cls.get_resource_url_list = staticmethod(curry(get_resource_url_list,
                                                           opts, cls.get_resource_url_list))

        if hasattr(cls, 'get_resource_url_count'):
            cls.get_resource_url_count = update_wrapper(curry(get_resource_url_count, opts, cls.get_resource_url_count),
                                                        cls.get_resource_url_count)

        if hasattr(cls, 'get_resource_url_detail'):
            cls.get_resource_url_detail = update_wrapper(curry(get_resource_url_detail, opts, cls.get_resource_url_detail),
                                                         cls.get_resource_url_detail)

        signals.class_prepared.send(sender=cls)


class ROAModel(models.Model):
    """
    Model which access remote resources.
    """
    __metaclass__ = ROAModelBase

    @staticmethod
    def get_resource_url_list():
        raise Exception("Static method get_resource_url_list is not defined.")

    def get_resource_url_count(self):
        return u"%scount/" % (self.get_resource_url_list(),)

    def get_resource_url_detail(self):
        return u"%s%s/" % (self.get_resource_url_list(), self.pk)

    def save_base(self, raw=False, cls=None, origin=None, force_insert=False,
                  force_update=False, using=None, update_fields=None):
        """
        Does the heavy-lifting involved in saving. Subclasses shouldn't need to
        override this method. It's separate from save() in order to hide the
        need for overrides of save() to pass around internal-only parameters
        ('raw', 'cls', and 'origin').
        """
        assert not (force_insert and force_update)
        if cls is None:
            cls = self.__class__
            meta = cls._meta
            if not meta.proxy:
                origin = cls
        else:
            meta = cls._meta

        if origin and not getattr(meta, "auto_created", False):
            signals.pre_save.send(sender=origin, instance=self, raw=raw)

        model_name = str(meta)

        # If we are in a raw save, save the object exactly as presented.
        # That means that we don't try to be smart about saving attributes
        # that might have come from the parent class - we just save the
        # attributes we have been given to the class we have been given.
        # We also go through this process to defer the save of proxy objects
        # to their actual underlying model.
        if not raw or meta.proxy:
            if meta.proxy:
                org = cls
            else:
                org = None
            for parent, field in meta.parents.items():
                # At this point, parent's primary key field may be unknown
                # (for example, from administration form which doesn't fill
                # this field). If so, fill it.
                if field and getattr(self, parent._meta.pk.attname) is None and getattr(self, field.attname) is not None:
                    setattr(self, parent._meta.pk.attname, getattr(self, field.attname))

                self.save_base(cls=parent, origin=org, using=using)

                if field:
                    setattr(self, field.attname, self._get_pk_val(parent._meta))
            if meta.proxy:
                return

        if not meta.proxy:
            pk_val = self._get_pk_val(meta)
            pk_is_set = pk_val is not None

            get_args = {'format': ROA_FORMAT}
            get_args.update(ROA_CUSTOM_ARGS)

            serializer = self.get_serializer(self)
            payload = self.get_renderer().render(serializer.data)

            # check if resource use custom primary key
            if not meta.pk.attname in ['pk', 'id']:
                # consider it might be inserting so check it first
                # @todo: try to improve this block to check if custom pripary key is not None first
                resource = Resource(self.get_resource_url_detail(),
                                    filters=ROA_FILTERS)
                try:
                    response = resource.get(payload=None, headers=ROA_HEADERS, **get_args)
                except ResourceNotFound:
                    # since such resource does not exist, it's actually creating
                    pk_is_set = False
                except RequestFailed:
                    pk_is_set = False

            if force_update or pk_is_set and not self.pk is None:
                record_exists = True
                resource = Resource(self.get_resource_url_detail(),
                                    filters=ROA_FILTERS)
                try:
                    logger.debug(u"""Modifying : "%s" through %s
                                  with payload "%s" and GET args "%s" """ % (
                                  force_unicode(self),
                                  force_unicode(resource.uri),
                                  force_unicode(payload),
                                  force_unicode(get_args)))
                    response = resource.put(payload=payload, headers=ROA_HEADERS, **get_args)
                except RequestFailed as e:
                    raise ROAException(e)
            else:
                record_exists = False
                resource = Resource(self.get_resource_url_list(),
                                    filters=ROA_FILTERS)
                try:
                    logger.debug(u"""Creating  : "%s" through %s
                                  with payload "%s" and GET args "%s" """ % (
                                  force_unicode(self),
                                  force_unicode(resource.uri),
                                  force_unicode(payload),
                                  force_unicode(get_args)))
                    response = resource.post(payload=payload, headers=ROA_HEADERS, **get_args)
                except RequestFailed as e:
                    raise ROAException(e)

            response = force_unicode(response.body_string()).encode(DEFAULT_CHARSET)

            for local_name, remote_name in ROA_MODEL_NAME_MAPPING:
                response = response.replace(remote_name, local_name)

            parser = self.get_parser()
            serializer = self.get_serializer(data=parser.parse(StringIO(response)))

            if not serializer.is_valid():
                raise ROAException('Invalid deserialization')

            self = serializer.object

        if origin:
            signals.post_save.send(sender=origin, instance=self,
                created=(not record_exists), raw=raw)

    save_base.alters_data = True

    def delete(self):
        assert self._get_pk_val() is not None, "%s object can't be deleted " \
                "because its %s attribute is set to None." \
                % (self._meta.object_name, self._meta.pk.attname)

        # Deletion in cascade should be done server side.
        resource = Resource(self.get_resource_url_detail(),
                            filters=ROA_FILTERS)

        logger.debug(u"""Deleting  : "%s" through %s""" % \
            (unicode(self), unicode(resource.uri)))

        resource.delete(headers=ROA_HEADERS, **ROA_CUSTOM_ARGS)

    delete.alters_data = True

    def _get_unique_checks(self, exclude=None):
        """
        We don't want to check unicity that way for now.
        """
        unique_checks, date_checks = [], []
        return unique_checks, date_checks


##############################################
# HELPER FUNCTIONS (CURRIED MODEL FUNCTIONS) #
##############################################

ROA_URL_OVERRIDES_LIST = getattr(settings, 'ROA_URL_OVERRIDES_LIST', {})
ROA_URL_OVERRIDES_COUNT = getattr(settings, 'ROA_URL_OVERRIDES_COUNT', {})
ROA_URL_OVERRIDES_DETAIL = getattr(settings, 'ROA_URL_OVERRIDES_DETAIL', {})


def get_resource_url_list(opts, func, *args, **kwargs):
    key = '%s.%s' % (opts.app_label, opts.module_name)
    overridden = ROA_URL_OVERRIDES_LIST.get(key, False)
    return overridden and overridden or func(*args, **kwargs)


def get_resource_url_count(opts, func, self, *args, **kwargs):
    key = '%s.%s' % (opts.app_label, opts.module_name)
    return ROA_URL_OVERRIDES_COUNT.get(key, func)(self, *args, **kwargs)


def get_resource_url_detail(opts, func, self, *args, **kwargs):
    key = '%s.%s' % (opts.app_label, opts.module_name)
    return ROA_URL_OVERRIDES_DETAIL.get(key, func)(self, *args, **kwargs)
