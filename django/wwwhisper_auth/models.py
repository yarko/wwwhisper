"""Data model underlying access control mechanism.

Stores information about locations, users and permission. Provides
methods that map to REST operations that can be perfomed on users,
locations and permissions resources. Allows to retrieve externally
visible attributes of these resources, the attributes are returned as
a resource representation by REST methods.

Makes sure entered emails and paths are valid. Allows to determine if
a user can access a given path.
"""

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.forms import ValidationError
from wwwhisper_auth import  url_path

import re
import uuid

SITE_URL = getattr(settings, 'SITE_URL', None)
if SITE_URL is None:
    raise ImproperlyConfigured(
        'WWWhisper requires SITE_URL to be set in django settings.py file');

class CreationException(Exception):
    """Raised when creation of a new location or user failed."""
    pass;

class ValidatedModel(models.Model):
    """Base class for all model classes.

    Makes sure all constraints are preserved before changed data is
    saved.
    """

    def save(self, *args, **kwargs):
        self.full_clean()
        return super(ValidatedModel, self).save(*args, **kwargs)

    class Meta:
        # Do not create a DB table for ValidatedModel.
        abstract = True

# Because Django authentication mechanism is used, users need to be
# represented by a standard Django User class. But some additions are
# needed:

"""Externaly visible UUID of a user.

Allows to identify a REST resource representing a user. UUID is stored
in the username field when User object is created. Standard primary
key ids are not used for external identification purposes, because
those ids can be reused after object is deleted."""
User.uuid = property(lambda(self): self.username)

"""Returns externaly visible attributes of the user resource."""
User.attributes_dict = lambda(self): \
    _add_common_attributes(self, {'email': self.email})


class Location(ValidatedModel):
    """A location for which access control rules are defined.

    Location is uniquely identified by its canonical path. All access
    control rules defined for a location apply also to sub-paths,
    unless a more specific location exists. In such case the more
    specific location takes precedence over the more generic one.

    For example, if a location with a path /pub is defined and a user
    foo@example.com is granted access to this location, the user can
    access /pub and all sub path of /pub. But if a location with a
    path /pub/beer is added, and the user foo@example.com is not
    granted access to this location, the user won't be able to access
    /pub/beer and all its sub-paths.

    Attributes:
      path: Canonical path of the location.
      uuid: Externally visible UUID of the location, allows to identify a REST
          resource representing the location.
      open_access: If true, access to the location does not require
          authentication.
    """

    path = models.CharField(max_length=2000, null=False, primary_key=True)
    uuid = models.CharField(max_length=36, null=False, db_index=True,
                            editable=False)
    open_access = models.BooleanField(default=False, null=False)

    def grant_open_access(self):
        """Allows access to the location without authentication.

        For authenticated users, access is also always allowed.
        """
        self.open_access = True;
        self.save();

    def revoke_open_access(self):
        """Disallows access to the location without authentication."""
        self.open_access= False
        self.save();

    def can_access(self, user_uuid):
        """Determines if a user can access the location.

        Args:
            user_uuid: string UUID of a user.

        Returns:
            True if the user is granted permission to access the
            location or it the location is open.
        """
        return (self.open_access
                or _find(Permission,
                         user__username=user_uuid,
                         http_location=self.path) is not None)

    def grant_access(self, user_uuid):
        """Allows access to the location by a given user.

        Args:
            user_uuid: string UUID of a user.

        Returns:
            (new Permission object, True) if access to the location was
                sucesfully granted.
            (existing Permission object, False) if user already had
                granted access to the location.

        Raises:
            LookupError: No user with a given UUID.
        """
        user = _find(User, username=user_uuid)
        if user is None:
            raise LookupError('User not found')
        permission = _find(
            Permission, http_location_id=self.path, user_id=user.id)
        created = False
        if permission is None:
            created = True
            permission = Permission.objects.create(
                http_location_id=self.path, user_id=user.id)
            permission.save()
        return (permission, created)

    def revoke_access(self, user_uuid):
        """Disallows access to the location by a given user.

        Args:
            user_uuid: string UUID of a user.

        Raises:
            LookupError: No user with a given UUID or the user can not
                access the location.
        """
        permission = self.get_permission(user_uuid)
        permission.delete()

    def get_permission(self, user_uuid):
        """Gets Permission object for a given user.

        Args:
            user_uuid: string UUID of a user.

        Raises:
            LookupError: No user with a given UUID or the user can not
                access the location.
        """
        user = _find(User, username=user_uuid)
        if user is None:
            raise LookupError('User not found.')
        permission = _find(
            Permission, http_location_id=self.path, user_id=user.id)
        if permission is None:
            raise LookupError('User can not access location.')
        return permission

    def allowed_users(self):
        """"Returns a list of users that can access the location."""
        return [permission.user for permission in
                Permission.objects.filter(http_location=self.path)]

    def attributes_dict(self):
        """Returns externally visible attributes of the location resource."""
        return _add_common_attributes(self, {
                'path': self.path,
                'openAccess': self.open_access,
                'allowedUsers': [
                    user.attributes_dict() for user in self.allowed_users()
                    ],
                })

    @models.permalink
    def get_absolute_url(self):
        """Constructs URL of the location resource."""
        return ('wwwhisper_location', (), {'uuid' : self.uuid})

    def save(self, *args, **kwargs):
        if not self.uuid:
            self.uuid = str(uuid.uuid4())
        return super(Location, self).save(*args, **kwargs)

    def __unicode__(self):
        return "%s" % (self.path)

class Permission(ValidatedModel):
    http_location = models.ForeignKey(Location)
    user = models.ForeignKey(User)

    def attributes_dict(self):
        return _add_common_attributes(
            self, {'user': self.user.attributes_dict()})

    @models.permalink
    def get_absolute_url(self):
        return ('wwwhisper_allowed_user', (),
                {'location_uuid' : self.http_location.uuid,
                 'user_uuid': self.user.uuid})

    def __unicode__(self):
        return "%s, %s" % (self.http_location, self.user.email)

class Collection(object):
    def all(self):
        return self.model_class.objects.all()

    def find_item(self, uuid):
        filter_args = {self.uuid_column_name: uuid}
        return _find(self.model_class, **filter_args)

    def delete_item(self, uuid):
        item = self.find_item(uuid)
        if item is None:
            return False
        item.delete()
        return True

class UsersCollection(Collection):
    collection_name = 'users'
    item_name = 'user'
    model_class = User
    uuid_column_name = 'username'

    def create_item(self, email):
        try:
            encoded_email = _encode_email(email)
        except ValidationError, ex:
            raise CreationException(ex)
        if _find(User, email=encoded_email) is not None:
            raise CreationException('User already exists.')
        user = User.objects.create(
            username=str(uuid.uuid4()), email=encoded_email, is_active=True)
        return user

    def find_item_by_email(self, email):
        try:
            encoded_email = _encode_email(email)
        except ValidationError, ex:
            return None
        return _find(self.model_class, email=encoded_email);

class LocationsCollection(Collection):
    collection_name = 'locations'
    item_name = 'location'
    model_class = Location
    uuid_column_name = 'uuid'

    def create_item(self, path):
        if not url_path.is_canonical(path):
            raise CreationException(
                'Path should be absolute and normalized (starting with / '\
                    'without /../ or /./ or //).')
        if url_path.contains_fragment(path):
            raise CreationException(
                "Path should not contain fragment ('#' part).")
        if url_path.contains_query(path):
            raise CreationException(
                "Path should not contain query ('?' part).")
        if url_path.contains_params(path):
            raise CreationException(
                "Path should not contain parameters (';' part).")
        if _find(Location, path=path) is not None:
            raise CreationException('Location already exists.')
        location = Location.objects.create(path=path)
        location.save()
        return location


    def find_parent(self, normalized_path):
        normalized_path_len = len(normalized_path)
        longest_matched_location = None
        longest_matched_location_len = -1

        for location in Location.objects.all():
            probed_path = location.path
            probed_path_len = len(probed_path)
            trailing_slash_index = None
            if probed_path[probed_path_len - 1] == '/':
                trailing_slash_index = probed_path_len - 1
            else:
                trailing_slash_index = probed_path_len

            if (normalized_path.startswith(probed_path) and
                probed_path_len > longest_matched_location_len and
                (probed_path_len == normalized_path_len or
                 normalized_path[trailing_slash_index] == '/')) :
                longest_matched_location_len = probed_path_len
                longest_matched_location = location
        return longest_matched_location

def full_url(absolute_path):
    return SITE_URL + absolute_path

def _urn_from_uuid(uuid):
    return 'urn:uuid:' + uuid

def _add_common_attributes(item, attributes_dict):
    attributes_dict['self'] = full_url(item.get_absolute_url())
    if hasattr(item, 'uuid'):
        attributes_dict['id'] = _urn_from_uuid(item.uuid)
    return attributes_dict

def _find(model_class, **kwargs):
    item = model_class.objects.filter(**kwargs)
    count = item.count()
    assert count <= 1
    if count == 0:
        return None
    return item.get()

def _encode_email(email):
    encoded_email = email.lower()
    if not _is_email_valid(encoded_email):
        raise ValidationError('Invalid email format.')
    return encoded_email

def _is_email_valid(email):
    """Validates email with regexp defined by BrowserId:
    browserid/browserid/static/dialog/resources/validation.js
    """
    return re.match(
        "^[\w.!#$%&'*+\-/=?\^`{|}~]+@[a-z0-9-]+(\.[a-z0-9-]+)+$",
        email) != None
