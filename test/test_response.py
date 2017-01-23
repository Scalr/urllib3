import unittest
import socket

import h11

from io import BytesIO, BufferedReader

from urllib3.connection import OldHTTPResponse
from urllib3.response import HTTPResponse
from urllib3.exceptions import (
    DecodeError, ProtocolError, InvalidHeader
)
from urllib3.util.retry import Retry
from urllib3.util.response import is_fp_closed

from base64 import b64decode

# A known random (i.e, not-too-compressible) payload generated with:
#    "".join(random.choice(string.printable) for i in xrange(512))
#    .encode("zlib").encode("base64")
# Randomness in tests == bad, and fixing a seed may not be sufficient.
ZLIB_PAYLOAD = b64decode(b"""\
eJwFweuaoQAAANDfineQhiKLUiaiCzvuTEmNNlJGiL5QhnGpZ99z8luQfe1AHoMioB+QSWHQu/L+
lzd7W5CipqYmeVTBjdgSATdg4l4Z2zhikbuF+EKn69Q0DTpdmNJz8S33odfJoVEexw/l2SS9nFdi
pis7KOwXzfSqarSo9uJYgbDGrs1VNnQpT9f8zAorhYCEZronZQF9DuDFfNK3Hecc+WHLnZLQptwk
nufw8S9I43sEwxsT71BiqedHo0QeIrFE01F/4atVFXuJs2yxIOak3bvtXjUKAA6OKnQJ/nNvDGKZ
Khe5TF36JbnKVjdcL1EUNpwrWVfQpFYJ/WWm2b74qNeSZeQv5/xBhRdOmKTJFYgO96PwrHBlsnLn
a3l0LwJsloWpMbzByU5WLbRE6X5INFqjQOtIwYz5BAlhkn+kVqJvWM5vBlfrwP42ifonM5yF4ciJ
auHVks62997mNGOsM7WXNG3P98dBHPo2NhbTvHleL0BI5dus2JY81MUOnK3SGWLH8HeWPa1t5KcW
S5moAj5HexY/g/F8TctpxwsvyZp38dXeLDjSQvEQIkF7XR3YXbeZgKk3V34KGCPOAeeuQDIgyVhV
nP4HF2uWHA==""")


def old_response(socket):
    return OldHTTPResponse(
        socket, state_machine=h11.Connection(our_role=h11.CLIENT)
    )


def chunked_response(socket):
    """
    Prepares an OldHTTPResponse ready for use with chunked
    transfer encoding.
    """
    # Prime the state machine.
    conn = h11.Connection(our_role=h11.CLIENT)
    conn.receive_data(
        b'HTTP/1.1 200 OK\r\n'
        b'Server: no such server\r\n'
        b'Transfer-Encoding: chunked\r\n'
        b'\r\n'
    )
    conn.next_event()
    return OldHTTPResponse(socket, state_machine=conn)


class TestLegacyResponse(unittest.TestCase):
    def test_getheaders(self):
        headers = {'host': 'example.com'}
        r = HTTPResponse(headers=headers)
        self.assertEqual(r.getheaders(), headers)

    def test_getheader(self):
        headers = {'host': 'example.com'}
        r = HTTPResponse(headers=headers)
        self.assertEqual(r.getheader('host'), 'example.com')


class TestResponse(unittest.TestCase):
    def test_cache_content(self):
        r = HTTPResponse('foo')
        self.assertEqual(r.data, 'foo')
        self.assertEqual(r._body, 'foo')

    def test_default(self):
        r = HTTPResponse()
        self.assertEqual(r.data, b'')

    def test_none(self):
        r = HTTPResponse(None)
        self.assertEqual(r.data, b'')

    def test_preload(self):
        fp = BytesIO(b'foo')

        r = HTTPResponse(fp, preload_content=True)

        self.assertEqual(fp.tell(), len(b'foo'))
        self.assertEqual(r.data, b'foo')

    def test_no_preload(self):
        fp = BytesIO(b'foo')

        r = HTTPResponse(fp, preload_content=False)

        self.assertEqual(fp.tell(), 0)
        self.assertEqual(r.data, b'foo')
        self.assertEqual(fp.tell(), len(b'foo'))

    def test_decode_bad_data(self):
        fp = BytesIO(b'\x00' * 10)
        self.assertRaises(DecodeError, HTTPResponse, fp, headers={
            'content-encoding': 'deflate'
        })

    def test_reference_read(self):
        fp = BytesIO(b'foo')
        r = HTTPResponse(fp, preload_content=False)

        self.assertEqual(r.read(1), b'f')
        self.assertEqual(r.read(2), b'oo')
        self.assertEqual(r.read(), b'')
        self.assertEqual(r.read(), b'')

    def test_decode_deflate(self):
        import zlib
        data = zlib.compress(b'foo')

        fp = BytesIO(data)
        r = HTTPResponse(fp, headers={'content-encoding': 'deflate'})

        self.assertEqual(r.data, b'foo')

    def test_decode_deflate_case_insensitve(self):
        import zlib
        data = zlib.compress(b'foo')

        fp = BytesIO(data)
        r = HTTPResponse(fp, headers={'content-encoding': 'DeFlAtE'})

        self.assertEqual(r.data, b'foo')

    def test_chunked_decoding_deflate(self):
        import zlib
        data = zlib.compress(b'foo')

        fp = BytesIO(data)
        r = HTTPResponse(fp, headers={'content-encoding': 'deflate'},
                         preload_content=False)

        self.assertEqual(r.read(1), b'f')
        self.assertEqual(r.read(2), b'oo')
        self.assertEqual(r.read(), b'')
        self.assertEqual(r.read(), b'')


    def test_chunked_decoding_deflate2(self):
        import zlib
        compress = zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS)
        data = compress.compress(b'foo')
        data += compress.flush()

        fp = BytesIO(data)
        r = HTTPResponse(fp, headers={'content-encoding': 'deflate'},
                         preload_content=False)

        self.assertEqual(r.read(1), b'f')
        self.assertEqual(r.read(2), b'oo')
        self.assertEqual(r.read(), b'')
        self.assertEqual(r.read(), b'')


    def test_chunked_decoding_gzip(self):
        import zlib
        compress = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        data = compress.compress(b'foo')
        data += compress.flush()

        fp = BytesIO(data)
        r = HTTPResponse(fp, headers={'content-encoding': 'gzip'},
                         preload_content=False)

        self.assertEqual(r.read(1), b'f')
        self.assertEqual(r.read(2), b'oo')
        self.assertEqual(r.read(), b'')
        self.assertEqual(r.read(), b'')


    def test_body_blob(self):
        resp = HTTPResponse(b'foo')
        self.assertEqual(resp.data, b'foo')
        self.assertTrue(resp.closed)

    def test_io(self):
        fp = BytesIO(b'foo')
        resp = HTTPResponse(fp, preload_content=False)

        self.assertEqual(resp.closed, False)
        self.assertEqual(resp.readable(), True)
        self.assertEqual(resp.writable(), False)
        self.assertRaises(IOError, resp.fileno)

        resp.close()
        self.assertEqual(resp.closed, True)

        # Try closing with an `OldHTTPResponse`
        hlr = old_response(socket.socket())
        resp2 = HTTPResponse(hlr, preload_content=False)
        self.assertEqual(resp2.closed, False)
        resp2.close()
        self.assertEqual(resp2.closed, True)

        #also try when only data is present.
        resp3 = HTTPResponse('foodata')
        self.assertRaises(IOError, resp3.fileno)

    def test_io_bufferedreader(self):
        fp = BytesIO(b'foo')
        resp = HTTPResponse(fp, preload_content=False)
        br = BufferedReader(resp)

        self.assertEqual(br.read(), b'foo')

        br.close()
        self.assertEqual(resp.closed, True)

        b = b'fooandahalf'
        fp = BytesIO(b)
        resp = HTTPResponse(fp, preload_content=False)
        br = BufferedReader(resp, 5)

        # This is necessary to make sure the "no bytes left" part of `readinto`
        # gets tested.
        while not br.closed:
            self.assertEqual(len(br.read(5)), 5)

    def test_streaming(self):
        fp = BytesIO(b'foo')
        resp = HTTPResponse(fp, preload_content=False)
        stream = resp.stream(2, decode_content=False)

        self.assertEqual(next(stream), b'fo')
        self.assertEqual(next(stream), b'o')
        self.assertRaises(StopIteration, next, stream)

    def test_streaming_tell(self):
        fp = BytesIO(b'foo')
        resp = HTTPResponse(fp, preload_content=False)
        stream = resp.stream(2, decode_content=False)

        position = 0

        position += len(next(stream))
        self.assertEqual(2, position)
        self.assertEqual(position, resp.tell())

        position += len(next(stream))
        self.assertEqual(3, position)
        self.assertEqual(position, resp.tell())

        self.assertRaises(StopIteration, next, stream)

    def test_gzipped_streaming(self):
        import zlib
        compress = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        data = compress.compress(b'foo')
        data += compress.flush()

        fp = BytesIO(data)
        resp = HTTPResponse(fp, headers={'content-encoding': 'gzip'},
                         preload_content=False)
        stream = resp.stream(2)

        self.assertEqual(next(stream), b'fo')
        self.assertEqual(next(stream), b'o')
        self.assertRaises(StopIteration, next, stream)

    def test_gzipped_streaming_tell(self):
        import zlib
        compress = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        uncompressed_data = b'foo'
        data = compress.compress(uncompressed_data)
        data += compress.flush()

        fp = BytesIO(data)
        resp = HTTPResponse(fp, headers={'content-encoding': 'gzip'},
                         preload_content=False)
        stream = resp.stream()

        # Read everything
        payload = next(stream)
        self.assertEqual(payload, uncompressed_data)

        self.assertEqual(len(data), resp.tell())

        self.assertRaises(StopIteration, next, stream)

    def test_deflate_streaming_tell_intermediate_point(self):
        # Ensure that ``tell()`` returns the correct number of bytes when
        # part-way through streaming compressed content.
        import zlib

        NUMBER_OF_READS = 10

        class MockCompressedDataReading(BytesIO):
            """
            A ByteIO-like reader returning ``payload`` in ``NUMBER_OF_READS``
            calls to ``read``.
            """

            def __init__(self, payload, payload_part_size):
                self.payloads = [
                    payload[i*payload_part_size:(i+1)*payload_part_size]
                    for i in range(NUMBER_OF_READS+1)]

                assert b"".join(self.payloads) == payload

            def read(self, _):
                # Amount is unused.
                if len(self.payloads) > 0:
                    return self.payloads.pop(0)
                return b""

        uncompressed_data = zlib.decompress(ZLIB_PAYLOAD)

        payload_part_size = len(ZLIB_PAYLOAD) // NUMBER_OF_READS
        fp = MockCompressedDataReading(ZLIB_PAYLOAD, payload_part_size)
        resp = HTTPResponse(fp, headers={'content-encoding': 'deflate'},
                            preload_content=False)
        stream = resp.stream()

        parts_positions = [(part, resp.tell()) for part in stream]
        end_of_stream = resp.tell()

        self.assertRaises(StopIteration, next, stream)

        parts, positions = zip(*parts_positions)

        # Check that the payload is equal to the uncompressed data
        payload = b"".join(parts)
        self.assertEqual(uncompressed_data, payload)

        # Check that the positions in the stream are correct
        expected = [(i+1)*payload_part_size for i in range(NUMBER_OF_READS)]
        self.assertEqual(expected, list(positions))

        # Check that the end of the stream is in the correct place
        self.assertEqual(len(ZLIB_PAYLOAD), end_of_stream)

    def test_deflate_streaming(self):
        import zlib
        data = zlib.compress(b'foo')

        fp = BytesIO(data)
        resp = HTTPResponse(fp, headers={'content-encoding': 'deflate'},
                         preload_content=False)
        stream = resp.stream(2)

        self.assertEqual(next(stream), b'fo')
        self.assertEqual(next(stream), b'o')
        self.assertRaises(StopIteration, next, stream)

    def test_deflate2_streaming(self):
        import zlib
        compress = zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS)
        data = compress.compress(b'foo')
        data += compress.flush()

        fp = BytesIO(data)
        resp = HTTPResponse(fp, headers={'content-encoding': 'deflate'},
                         preload_content=False)
        stream = resp.stream(2)

        self.assertEqual(next(stream), b'fo')
        self.assertEqual(next(stream), b'o')
        self.assertRaises(StopIteration, next, stream)

    def test_empty_stream(self):
        fp = BytesIO(b'')
        resp = HTTPResponse(fp, preload_content=False)
        stream = resp.stream(2, decode_content=False)

        self.assertRaises(StopIteration, next, stream)

    def test_mock_httpresponse_stream(self):
        # Mock out a HTTP Request that does enough to make it through urllib3's
        # read() and close() calls, and also exhausts and underlying file
        # object.
        class MockHTTPRequest(object):
            self.fp = None

            def read(self, amt):
                data = self.fp.read(amt)
                if not data:
                    self.fp = None

                return data

            def close(self):
                self.fp = None

        bio = BytesIO(b'foo')
        fp = MockHTTPRequest()
        fp.fp = bio
        resp = HTTPResponse(fp, preload_content=False)
        stream = resp.stream(2)

        self.assertEqual(next(stream), b'fo')
        self.assertEqual(next(stream), b'o')
        self.assertRaises(StopIteration, next, stream)

    def test_get_case_insensitive_headers(self):
        headers = {'host': 'example.com'}
        r = HTTPResponse(headers=headers)
        self.assertEqual(r.headers.get('host'), 'example.com')
        self.assertEqual(r.headers.get('Host'), 'example.com')

    def test_retries(self):
        fp = BytesIO(b'')
        resp = HTTPResponse(fp)
        self.assertEqual(resp.retries, None)
        retry = Retry()
        resp = HTTPResponse(fp, retries=retry)
        self.assertEqual(resp.retries, retry)


class MockSock(object):
    @classmethod
    def makefile(cls, *args, **kwargs):
        return


if __name__ == '__main__':
    unittest.main()
