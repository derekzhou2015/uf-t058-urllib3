from __future__ import annotations

import io
import sys
import typing
from base64 import b64encode
from enum import Enum

from ..exceptions import UnrewindableBodyError
from .util import to_bytes

if typing.TYPE_CHECKING:
    from typing import Final

# Pass as a value within ``headers`` to skip
# emitting some HTTP headers that are added automatically.
# The only headers that are supported are ``Accept-Encoding``,
# ``Host``, and ``User-Agent``.
SKIP_HEADER = "@@@SKIP_HEADER@@@"
SKIPPABLE_HEADERS = frozenset(["accept-encoding", "host", "user-agent"])

ACCEPT_ENCODING = "gzip,deflate"
try:
    try:
        import brotlicffi as _unused_module_brotli  # type: ignore[import-not-found] # noqa: F401
    except ImportError:
        import brotli as _unused_module_brotli  # type: ignore[import-not-found] # noqa: F401
except ImportError:
    pass
else:
    ACCEPT_ENCODING += ",br"

try:
    if sys.version_info >= (3, 14):
        from compression import zstd as _unused_module_zstd  # noqa: F401
    else:
        from backports import zstd as _unused_module_zstd  # noqa: F401
except ImportError:
    pass
else:
    ACCEPT_ENCODING += ",zstd"


class _TYPE_FAILEDTELL(Enum):
    token = 0


_FAILEDTELL: Final[_TYPE_FAILEDTELL] = _TYPE_FAILEDTELL.token

_TYPE_BODY_POSITION = typing.Union[int, _TYPE_FAILEDTELL]

# When sending a request with these methods we aren't expecting
# a body so don't need to set an explicit 'Content-Length: 0'
# The reason we do this in the negative instead of tracking methods
# which 'should' have a body is because unknown methods should be
# treated as if they were 'POST' which *does* expect a body.
_METHODS_NOT_EXPECTING_BODY = {"GET", "HEAD", "DELETE", "TRACE", "OPTIONS", "CONNECT"}


def make_headers(
    keep_alive: bool | None = None,
    accept_encoding: bool | list[str] | str | None = None,
    user_agent: str | None = None,
    basic_auth: str | None = None,
    proxy_basic_auth: str | None = None,
    disable_cache: bool | None = None,
    *,
    basic_auth_encoding: str = "latin-1",
    proxy_basic_auth_encoding: str = "latin-1",
) -> dict[str, str]:
    """
    Shortcuts for generating request headers.

    :param keep_alive:
        If ``True``, adds 'connection: keep-alive' header.

    :param accept_encoding:
        Can be a boolean, list, or string.
        ``True`` translates to 'gzip,deflate'.  If the dependencies for
        Brotli (either the ``brotli`` or ``brotlicffi`` package) and/or
        Zstandard (the ``backports.zstd`` package for Python before 3.14)
        algorithms are installed, then their encodings are
        included in the string ('br' and 'zstd', respectively).
        List will get joined by comma.
        String will be used as provided.

    :param user_agent:
        String representing the user-agent you want, such as
        "python-urllib3/0.6"

    :param basic_auth:
        Colon-separated username:password string for 'authorization: basic ...'
        auth header. Basic authentication transmits Base64-encoded bytes, not
        Unicode text, so the string is encoded with ``basic_auth_encoding``
        before Base64 encoding.

        Must contain decoded credentials (not percent-encoded).
        For URLs parsed with :func:`parse_url`, use :attr:`Url.auth_decoded_joined`
        to get the properly formatted string.

    :param basic_auth_encoding:
        Text encoding used to encode ``basic_auth`` before Base64 encoding.
        Defaults to "latin-1" for backward compatibility. Use "utf-8" only
        when the peer expects UTF-8 Basic authentication credentials.

        Must contain decoded credentials (not percent-encoded).
        For URLs parsed with :func:`parse_url`, use :attr:`Url.auth_decoded_joined`
        to get the properly formatted string.

    :param proxy_basic_auth:
        Colon-separated username:password string for 'proxy-authorization: basic ...'
        auth header. Basic authentication transmits Base64-encoded bytes, not
        Unicode text, so the string is encoded with ``proxy_basic_auth_encoding``
        before Base64 encoding.

        Must contain decoded credentials (not percent-encoded).
        For URLs parsed with :func:`parse_url`, use :attr:`Url.auth_decoded_joined`
        to get the properly formatted string.

    :param proxy_basic_auth_encoding:
        Text encoding used to encode ``proxy_basic_auth`` before Base64
        encoding. Defaults to "latin-1" for backward compatibility. Use
        "utf-8" only when the proxy expects UTF-8 Basic authentication
        credentials.

        Must contain decoded credentials (not percent-encoded).
        For URLs parsed with :func:`parse_url`, use :attr:`Url.auth_decoded_joined`
        to get the properly formatted string.

    :param disable_cache:
        If ``True``, adds 'cache-control: no-cache' header.

    Example:

    .. code-block:: python

        import urllib3

        print(urllib3.util.make_headers(keep_alive=True, user_agent="Batman/1.0"))
        # {'connection': 'keep-alive', 'user-agent': 'Batman/1.0'}
        print(urllib3.util.make_headers(accept_encoding=True))
        # {'accept-encoding': 'gzip,deflate'}
    """
    headers: dict[str, str] = {}
    if accept_encoding:
        if isinstance(accept_encoding, str):
            pass
        elif isinstance(accept_encoding, list):
            accept_encoding = ",".join(accept_encoding)
        else:
            accept_encoding = ACCEPT_ENCODING
        headers["accept-encoding"] = accept_encoding

    if user_agent:
        headers["user-agent"] = user_agent

    if keep_alive:
        headers["connection"] = "keep-alive"

    if basic_auth:
        headers["authorization"] = (
            f"Basic {b64encode(basic_auth.encode(basic_auth_encoding)).decode()}"
        )

    if proxy_basic_auth:
        headers["proxy-authorization"] = (
            f"Basic {b64encode(proxy_basic_auth.encode(proxy_basic_auth_encoding)).decode()}"
        )

    if disable_cache:
        headers["cache-control"] = "no-cache"

    return headers


def set_file_position(
    body: typing.Any, pos: _TYPE_BODY_POSITION | None
) -> _TYPE_BODY_POSITION | None:
    """
    If a position is provided, move file to that point.
    Otherwise, we'll attempt to record a position for future use.
    """
    if pos is not None:
        rewind_body(body, pos)
    elif getattr(body, "tell", None) is not None:
        try:
            pos = body.tell()
        except OSError:
            # This differentiates from None, allowing us to catch
            # a failed `tell()` later when trying to rewind the body.
            pos = _FAILEDTELL

    return pos


def rewind_body(body: typing.IO[typing.AnyStr], body_pos: _TYPE_BODY_POSITION) -> None:
    """
    Attempt to rewind body to a certain position.
    Primarily used for request redirects and retries.

    :param body:
        File-like object that supports seek.

    :param int pos:
        Position to seek to in file.
    """
    body_seek = getattr(body, "seek", None)
    if body_seek is not None and isinstance(body_pos, int):
        try:
            body_seek(body_pos)
        except OSError as e:
            raise UnrewindableBodyError(
                "An error occurred when rewinding request body for redirect/retry."
            ) from e
    elif body_pos is _FAILEDTELL:
        raise UnrewindableBodyError(
            "Unable to record file position for rewinding "
            "request body during a redirect/retry."
        )
    elif body_seek is None:
        raise UnrewindableBodyError("body does not implement seek.")
    else:
        raise ValueError(
            f"body_pos must be of type integer, instead it was {type(body_pos)}."
        )


class ChunksAndContentLength(typing.NamedTuple):
    chunks: typing.Iterable[bytes] | None
    content_length: int | None


def is_replayable_body(body: typing.Any) -> bool:
    """
    Whether ``body`` can safely be sent more than once.

    ``bytes``, ``str``, objects implementing the buffer protocol and
    re-iterable containers (such as ``list``) produce a fresh iterable on every
    attempt and can always be replayed. File-like objects are handled by
    :func:`set_file_position` and :func:`rewind_body`, which record and restore
    their position, so they are reported as replayable here.

    One-shot iterators, such as generators and the result of ``iter([...])``,
    cannot be replayed: once an attempt has consumed them, a second attempt
    would send an empty body without anybody noticing.
    """
    if body is None or isinstance(body, (str, bytes)) or hasattr(body, "read"):
        return True

    try:
        # Same detection as body_to_chunks(): buffer-protocol objects can be
        # sent as-is on every attempt.
        memoryview(body)
    except TypeError:
        pass
    else:
        return True

    try:
        # An iterator is its own iterator, and can therefore be walked once.
        return iter(body) is not body
    except TypeError:
        # Not iterable at all; body_to_chunks() reports that on its own.
        return True


class _ReplayableBodyRecorder:
    """
    Tracks whether a one-shot iterator body has actually been consumed.

    A retry after a connection error happens before the body is ever read, so
    the same iterator can still be sent in full. Only once an attempt starts
    iterating the body does replay become impossible. This wrapper observes
    that first consumption; whoever retries or follows redirects creates one
    per request and carries it across attempts, and
    :func:`ensure_replayable_body` refuses to re-send a body that this wrapper
    has seen consumed.

    Bodies that are replayable on their own are never wrapped, so the common
    paths are untouched.
    """

    __slots__ = ("body", "consumed")

    def __init__(self, body: typing.Iterable[typing.Any]) -> None:
        self.body = body
        self.consumed = False

    def __iter__(self) -> typing.Iterator[typing.Any]:
        for chunk in self.body:
            # Any attempt that pulls a chunk has consumed the underlying
            # iterator, whether or not the chunk reaches the server.
            self.consumed = True
            yield chunk

    def __repr__(self) -> str:
        return f"<replayable body recorder for {self.body!r}>"

    def mark_consumed(self) -> None:
        """
        Record that the body may have been consumed.

        Backends that cannot observe iteration (the Emscripten bridge hands the
        object to JavaScript) call this before sending, so a later retry is
        refused instead of gambling on an empty body.
        """
        self.consumed = True

    def ensure_not_consumed(self) -> None:
        if self.consumed:
            raise UnrewindableBodyError(
                "Unable to re-send the request body for a retry or redirect: "
                "the body is an iterator that has already been consumed, so "
                "the next attempt would send an empty body. Pass a bytes-like "
                "object, a re-iterable container, or a seekable file-like "
                "object as the body to make the request replayable."
            )


def make_replayable_body(body: typing.Any) -> typing.Any:
    """
    Wrap a one-shot iterator body so consumption can be detected later.

    Returns ``body`` unchanged when it is already replayable or already
    wrapped, which makes this safe to call at every entry point that may retry
    or follow redirects.
    """
    if isinstance(body, _ReplayableBodyRecorder) or is_replayable_body(body):
        return body
    return _ReplayableBodyRecorder(body)


def unwrap_replayable_body(body: typing.Any) -> typing.Any:
    """
    Return the body a :func:`make_replayable_body` wrapper stands for.

    Backends that hand the body to something other than a Python iterator (for
    example the Emscripten fetch bridge) use this to keep receiving the exact
    object the caller passed. Such backends must also call
    :meth:`_ReplayableBodyRecorder.mark_consumed` once they have sent it,
    because they cannot record consumption any other way.
    """
    if isinstance(body, _ReplayableBodyRecorder):
        return body.body
    return body


def mark_replayable_body_sent(body: typing.Any) -> None:
    """
    Declare that ``body`` has been handed to a backend that consumed it.

    Backends which never iterate the body themselves (the Emscripten bridge
    passes it to JavaScript) call this once they have sent it, so a retry or
    redirect does not re-send an iterator that is already gone.
    """
    if isinstance(body, _ReplayableBodyRecorder):
        body.mark_consumed()


def ensure_replayable_body(body: typing.Any) -> None:
    """
    Refuse to re-send a request whose body cannot be replayed.

    Called right before a retry or redirect re-issues a request. Replaying a
    consumed iterator silently sends an empty body, which the server may accept
    with a successful status, so it is rejected explicitly instead.
    """
    if isinstance(body, _ReplayableBodyRecorder):
        body.ensure_not_consumed()


def body_to_chunks(
    body: typing.Any | None, method: str, blocksize: int
) -> ChunksAndContentLength:
    """Takes the HTTP request method, body, and blocksize and
    transforms them into an iterable of chunks to pass to
    socket.sendall() and an optional 'Content-Length' header.

    A 'Content-Length' of 'None' indicates the length of the body
    can't be determined so should use 'Transfer-Encoding: chunked'
    for framing instead.
    """

    chunks: typing.Iterable[bytes] | None
    content_length: int | None

    # No body, we need to make a recommendation on 'Content-Length'
    # based on whether that request method is expected to have
    # a body or not.
    if body is None:
        chunks = None
        if method.upper() not in _METHODS_NOT_EXPECTING_BODY:
            content_length = 0
        else:
            content_length = None

    # Bytes or strings become bytes
    elif isinstance(body, (str, bytes)):
        chunks = (to_bytes(body),)
        content_length = len(chunks[0])

    # File-like object, TODO: use seek() and tell() for length?
    elif hasattr(body, "read"):

        def chunk_readable() -> typing.Iterable[bytes]:
            encode = isinstance(body, io.TextIOBase)
            while True:
                datablock = body.read(blocksize)
                if not datablock:
                    break
                if encode:
                    datablock = datablock.encode("utf-8")
                yield datablock

        chunks = chunk_readable()
        content_length = None

    # Otherwise we need to start checking via duck-typing.
    else:
        try:
            # Check if the body implements the buffer API.
            mv = memoryview(body)
        except TypeError:
            try:
                # Check if the body is an iterable
                chunks = iter(body)
                content_length = None
            except TypeError:
                raise TypeError(
                    f"'body' must be a bytes-like object, file-like "
                    f"object, or iterable. Instead was {body!r}"
                ) from None
        else:
            # Since it implements the buffer API can be passed directly to socket.sendall()
            chunks = (body,)
            content_length = mv.nbytes

    return ChunksAndContentLength(chunks=chunks, content_length=content_length)
