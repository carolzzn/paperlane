"""Small, bounded IPP client. No discovery or destination supplied by web users."""
import http.client
import struct
import urllib.parse


def attribute(tag, name, value):
    name = name.encode()
    if isinstance(value, dict):
        result = bytes([tag]) + struct.pack('>H', len(name)) + name + b'\0\0'
        for key, (child_tag, child_value) in value.items():
            result += attribute(0x4a, '', key) + attribute(child_tag, '', child_value)
        return result + attribute(0x37, '', b'')
    if isinstance(value, bool):
        value = bytes([value])
    elif isinstance(value, int):
        value = struct.pack('>i', value)
    elif isinstance(value, str):
        value = value.encode()
    return bytes([tag]) + struct.pack('>H', len(name)) + name + struct.pack('>H', len(value)) + value


def decode(data):
    if len(data) < 8:
        raise ValueError('打印机返回了不完整的 IPP 响应')
    status = struct.unpack('>H', data[2:4])[0]
    pos, groups, current, last = 8, [], {}, ''

    def value(tag, raw):
        if tag in (0x21, 0x23) and len(raw) == 4:
            return struct.unpack('>i', raw)[0]
        if tag == 0x22:
            return raw != b'\0'
        if tag == 0x32 and len(raw) == 9:
            x, y, units = struct.unpack('>iiB', raw)
            return {'x': x, 'y': y, 'units': units}
        if tag == 0x33 and len(raw) == 8:
            return list(struct.unpack('>ii', raw))
        return raw.decode('utf-8', errors='replace')

    def record():
        nonlocal pos
        if pos + 3 > len(data):
            raise ValueError('IPP 响应被截断')
        tag = data[pos]
        n = struct.unpack('>H', data[pos + 1:pos + 3])[0]
        pos += 3
        if pos + n + 2 > len(data):
            raise ValueError('IPP 名称被截断')
        name = data[pos:pos + n].decode(errors='replace')
        pos += n
        size = struct.unpack('>H', data[pos:pos + 2])[0]
        pos += 2
        if pos + size > len(data):
            raise ValueError('IPP 属性被截断')
        raw = data[pos:pos + size]
        pos += size
        return tag, name, raw

    def collection(depth=0):
        if depth > 12:
            raise ValueError('IPP 集合嵌套过深')
        result, member = {}, ''
        while pos < len(data):
            tag, name, raw = record()
            if tag == 0x37:
                return result
            if tag == 0x4a:
                member = raw.decode(errors='replace')
            else:
                v = collection(depth + 1) if tag == 0x34 else value(tag, raw)
                result.setdefault(member or name, []).append(v)
        raise ValueError('IPP 集合未结束')

    while pos < len(data):
        tag = data[pos]
        if tag == 3:
            break
        if tag <= 0x0f:
            pos += 1
            current, last = {}, ''
            groups.append((tag, current))
            continue
        tag, name, raw = record()
        last = name or last
        v = collection() if tag == 0x34 else value(tag, raw)
        current.setdefault(last, []).append(v)
    if status >= 0x0400:
        messages = [g.get('status-message', [''])[0] for _, g in groups]
        raise ValueError('打印机拒绝请求 (IPP %04x)：%s' % (status, ' '.join(messages)))
    return {'status': status, 'groups': groups, 'attributes': {k: v for _, g in groups for k, v in g.items()}}


class Printer:
    def __init__(self, uri, timeout=15):
        self.uri = uri
        self.url = urllib.parse.urlsplit(uri)
        if self.url.scheme not in ('ipp', 'ipps') or not self.url.hostname:
            raise ValueError('打印机地址必须是 ipp:// 或 ipps://')
        self.timeout = timeout

    def request(self, operation, operation_attrs=(), job_attrs=(), document=b''):
        body = struct.pack('>BBHI', 2, 0, operation, 1) + b'\x01'
        body += attribute(0x47, 'attributes-charset', 'utf-8')
        body += attribute(0x48, 'attributes-natural-language', 'en')
        body += attribute(0x45, 'printer-uri', self.uri)
        for args in operation_attrs:
            body += attribute(*args)
        if job_attrs:
            body += b'\x02'
            for args in job_attrs:
                body += attribute(*args)
        body += b'\x03' + document
        cls = http.client.HTTPSConnection if self.url.scheme == 'ipps' else http.client.HTTPConnection
        conn = cls(self.url.hostname, self.url.port or 631, timeout=self.timeout)
        try:
            conn.request('POST', self.url.path or '/', body, {'Content-Type': 'application/ipp'})
            response = conn.getresponse()
            payload = response.read(4 * 1024 * 1024 + 1)
            if response.status != 200 or len(payload) > 4 * 1024 * 1024:
                raise ValueError('打印机 HTTP 响应异常：%s' % response.status)
            return decode(payload)
        finally:
            conn.close()

    def capabilities(self):
        return self.request(0x000b)['attributes']

    def photo_attributes(self, media, landscape=False, dimensions=(10160, 15240)):
        return [(0x44, 'media', media), (0x34, 'media-col', {
            'media-size': (0x34, {'x-dimension': (0x21, dimensions[0]), 'y-dimension': (0x21, dimensions[1])}),
            'media-left-margin': (0x21, 0), 'media-right-margin': (0x21, 0),
            'media-top-margin': (0x21, 0), 'media-bottom-margin': (0x21, 0)}),
            (0x23, 'orientation-requested', 4 if landscape else 3),
            (0x23, 'print-quality', 5), (0x44, 'print-scaling', 'none'),
            (0x32, 'printer-resolution', struct.pack('>iiB', 300, 300, 3))]

    def validate(self, media, landscape=False, dimensions=(10160, 15240)):
        return self.request(0x0004, [(0x49, 'document-format', 'image/jpeg')], self.photo_attributes(media, landscape, dimensions))

    def print_jpeg(self, data, media, landscape=False, dimensions=(10160, 15240)):
        return self.request(0x0002, [(0x42, 'requesting-user-name', 'PhotoStudio'), (0x42, 'job-name', 'Family photo'), (0x49, 'document-format', 'image/jpeg')], self.photo_attributes(media, landscape, dimensions), data)['attributes']

    def job(self, job_id):
        return self.request(0x0009, [(0x21, 'job-id', job_id)])['attributes']
