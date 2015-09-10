import logging
import mimetypes
import os
import re
import pyrax
import socket

from httplib import HTTPException
from io import UnsupportedOperation
from ssl import SSLError
from pyrax.exceptions import (
    NoSuchObject,
    ServiceResponseFailure,
)

from django.core.files.base import File
from django.core.files.storage import get_storage_class, Storage
from django.utils.text import get_valid_filename

from .settings import CUMULUS

logger = logging.getLogger(__name__)

HEADER_PATTERNS = tuple(
    (re.compile(p), h)
    for p, h
    in CUMULUS.get('HEADERS', {})
)


class CloudFilesStorage(Storage):
    """
    Custom storage for Rackspace Cloud Files.
    """
    api_key = CUMULUS['API_KEY']
    container_name = CUMULUS['CONTAINER']
    max_retries = CUMULUS['MAX_RETRIES']
    pyrax_identity_type = CUMULUS['PYRAX_IDENTITY_TYPE']
    timeout = CUMULUS['TIMEOUT']
    ttl = CUMULUS['TTL']
    use_ssl = CUMULUS['USE_SSL']
    username = CUMULUS['USERNAME']

    def __init__(self, username=None, api_key=None, container=None, timeout=None,
                 max_retries=None, container_uri=None):
        """
        Initialize the settings for the and container.
        """
        if username is not None:
            self.username = username
        if api_key is not None:
            self.api_key = api_key
        if container is not None:
            self.container_name = container
        if timeout is not None:
            self.timeout = timeout
        if max_retries is not None:
            self.max_retries = max_retries
        if container_uri is not None:
            self._container_public_uri = container_uri
        elif 'CONTAINER_URI' in CUMULUS:
            self._container_public_uri = CUMULUS['CONTAINER_URI']

        pyrax.set_setting("identity_type", self.pyrax_identity_type)
        pyrax.set_credentials(self.username, self.api_key)

    def __getstate__(self):
        """
        Return a picklable representation of the storage.
        """
        return {
            "username": self.username,
            "api_key": self.api_key,
            "container_name": self.container_name,
            "timeout": self.timeout,
        }

    def _get_container(self):
        if not hasattr(self, '_container'):
            self._container = pyrax.cloudfiles.get_container(self.container_name)
        return self._container

    def _set_container(self, container):
        """Set the container (and, if needed, the configured TTL on
        it), making the container publicly available.

        """
        pyrax.cloudfiles.make_container_public(container.name, ttl=self.ttl)
        if hasattr(self, '_container_public_uri'):
            delattr(self, '_container_public_uri')
        self._container = container

    container = property(_get_container, _set_container)

    @property
    def container_url(self):
        if not hasattr(self, '_container_public_uri'):
            if self.use_ssl:
                self._container_public_uri = self.container.cdn_ssl_uri
            else:
                self._container_public_uri = self.container.cdn_uri
        return self._container_public_uri

    def _get_cloud_obj(self, name):
        """
        Helper function to get retrieve the requested Cloud Files Object.
        """
        tries = 0
        while True:
            try:
                return self.container.get_object(name)
            except (HTTPException, SSLError, ServiceResponseFailure), e:
                if tries >= self.max_retries:
                    raise
                tries += 1
                logger.warning('Failed to retrieve %s: %r (attempt %d/%d)' % (
                    name, e, tries, self.max_retries))

    def _open(self, name, mode='rb'):
        """
        Return the CloudFilesStorageFile.
        """
        return CloudFilesStorageFile(container=self, name=name)

    def _save(self, name, content):
        """
        Use the Cloud Files service to write ``content`` to a remote file
        (called ``name``).
        """
        (path, last) = os.path.split(name)

        # If the objects has a hash, it already exists. The hash is
        # md5 of the content. If the hash has not changed, do not send
        # the file over again.
        upload = True
        etag = pyrax.utils.get_checksum(content.file)
        try:
            cloud_obj = self._get_cloud_obj(name)
        except NoSuchObject:
            pass
        else:
            if cloud_obj.etag and cloud_obj.etag == etag:
                upload = False

        if upload:
            content.open()
            # If the content type is available, pass it in directly
            # rather than getting the cloud object to try to guess.
            if hasattr(content.file, 'content_type'):
                content_type = content.file.content_type
            elif hasattr(content, 'content_type'):
                content_type = content.content_type
            else:
                mime_type, encoding = mimetypes.guess_type(name)
                content_type = mime_type

            headers = self._headers_for(name)
            # Try uploading the file
            tries = 0
            while True:
                try:
                    cloud_obj = self.container.upload_file(
                        content.file,
                        name,
                        content_type,
                        etag,
                        headers=headers
                    )

                    break
                except (HTTPException, SSLError, ServiceResponseFailure, socket.error) as e:
                    if tries >= self.max_retries:
                        raise
                    tries += 1
                    logger.warning('Failed to send %s: %r (attempt %d/%d)' % (
                        name, e, tries, self.max_retries))
                    # re-init the content before retrying
                    if hasattr(content, 'seek'):
                        content.seek(0)
                else:
                    content.close()
        return name

    def _headers_for(self, name, header_patterns=HEADER_PATTERNS):
        headers = {}
        for pattern, pattern_headers in header_patterns:
            if pattern.match(name):
                headers.update(pattern_headers.copy())
        return headers

    def delete(self, name):
        """
        Deletes the specified file from the storage system.
        """, name
        tries = 0
        while True:
            try:
                self.container.delete_object(name)
                break
            except NoSuchObject:
                # It doesn't exist and that's ok
                break
            except (HTTPException, SSLError, ServiceResponseFailure) as e:
                if tries >= self.max_retries:
                    raise
                tries += 1
                logger.warning('Failed to delete %s: %r (attempt %d/%d)' % (
                    name, e, tries, self.max_retries))

    def exists(self, name):
        """
        Returns True if a file referenced by the given name already exists in
        the storage system, or False if the name is available for a new file.
        """
        try:
            self._get_cloud_obj(name)
            return True
        except NoSuchObject:
            return False

    def get_valid_name(self, name):
        """Returns a filename, based on the provided filename,
        that's suitable for use in the target storage system.

        """
        return get_valid_filename(name)

    def listdir(self, path):
        """Lists the contents of the specified path, returning a
        2-tuple; the first being directories, the second being a list
        of filenames.
        """
        if path and not path.endswith('/'):
            path = '%s/' % path

        files = [f.name for f in self.container.list_all(prefix=path)]
        return ([], files)

    def size(self, name):
        """
        Returns the total size, in bytes, of the file specified by name.
        """
        return self._get_cloud_obj(name).total_bytes

    def url(self, name):
        """
        Returns an absolute URL where the file's contents can be accessed
        directly by a web browser.
        """
        return '{container_url}/{name}'.format(
            container_url=self.container_url,
            name=name
        )

    def modified_time(self, name):
        # CloudFiles return modified date in different formats
        # depending on whether or not we pre-loaded objects.
        # When pre-loaded, timezone is not included but we
        # assume UTC. Since FileStorage returns localtime, and
        # collectstatic compares these dates, we need to depend
        # on dateutil to help us convert timezones.
        try:
            from dateutil import parser, tz
        except ImportError:
            raise NotImplementedError("This functionality requires dateutil to be installed")

        obj = self._get_cloud_obj(name)
        # convert string to date
        date = parser.parse(obj.last_modified)

        # if the date has no timezone, assume UTC
        if date.tzinfo is None:
            date = date.replace(tzinfo=tz.tzutc())

        # convert date to local time w/o timezone
        date = date.astimezone(tz.tzlocal()).replace(tzinfo=None)
        return date


class CloudFilesStaticStorage(CloudFilesStorage):
    """
    Subclasses CloudFilesStorage to automatically set the container to the one
    specified in CUMULUS['STATIC_CONTAINER']. This provides the ability to
    specify a separate storage backend for Django's collectstatic command.

    To use, make sure CUMULUS['STATIC_CONTAINER'] is set to something other
    than CUMULUS['CONTAINER']. Then, tell Django's staticfiles app by setting
    STATICFILES_STORAGE = 'cumulus.storage.CloudFilesStaticStorage'.
    """
    container_name = CUMULUS['STATIC_CONTAINER']


class CachedCloudFilesStaticStorage(CloudFilesStaticStorage):
    """
    Cloud Files storage backend that saves the files locally, too.
    """
    def __init__(self, *args, **kwargs):
        super(CachedCloudFilesStaticStorage, self).__init__(*args, **kwargs)
        self.local_storage = get_storage_class("compressor.storage.CompressorFileStorage")()

    def _save(self, name, content):
        name = super(CachedCloudFilesStaticStorage, self)._save(name, content)
        self.local_storage._save(name, content)
        return name


class CloudFilesStorageFile(File):
    closed = False

    def __init__(self, container, name, *args, **kwargs):
        self._container = container
        super(CloudFilesStorageFile, self).__init__(file=None, name=name,
                                                    *args, **kwargs)

    def _get_size(self):
        if not hasattr(self, '_size'):
            self._size = self._container.size(self.name)
        return self._size

    def _set_size(self, size):
        self._size = size
    size = property(_get_size, _set_size)

    def _get_file(self):
        if not hasattr(self, '_file'):
            self._file = self._container._get_cloud_obj(self.name)
            self._pos = 0
        return self._file

    def _set_file(self, value):
        if value is None:
            if hasattr(self, '_file'):
                del self._file
        else:
            self._file = value
    file = property(_get_file, _set_file)

    def __iter__(self):
        for chunk in self.chunks():
            yield chunk

    def chunks(self, chunk_size=None):
        """Read the file and yield chunks of ``chunk_size`` bytes
        (defaults to ``UploadedFile.DEFAULT_CHUNK_SIZE``).

        """
        if not chunk_size:
            chunk_size = self.DEFAULT_CHUNK_SIZE

        try:
            self.seek(0)
        except (AttributeError, UnsupportedOperation):
            pass

        while True:
            data = self.file.get(chunk_size=chunk_size)
            if not data:
                break
            yield data

    def read(self, num_bytes=0):
        if self._pos == self.size:
            return ""

        if num_bytes and self._pos + num_bytes > self.size:
            num_bytes = self.size - self._pos

        data = self.file.get()
        self._pos += len(data)
        return data
