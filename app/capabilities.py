"""Normalize IPP capabilities into explicit, immutable print tickets."""
import hashlib
import json
import math
import re


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def first(obj, key, default=None):
    values = obj.get(key, [])
    return values[0] if values else default


def dimension(value):
    return isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 100000


def media_keyword_size(name):
    match = re.search(r'_(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)(mm|in)$', name)
    if not match:
        return None
    scale = 2540 if match[3] == 'in' else 100
    return tuple(round(float(match[i]) * scale) for i in (1, 2))


def normalize(attrs):
    """Expose actual media-col combinations, not a Cartesian product."""
    formats = sorted(set(attrs.get('document-format-supported', [])))
    names = [n for n in attrs.get('media-supported', []) if isinstance(n, str)]
    ready = attrs.get('media-col-ready', [])
    database = attrs.get('media-col-database', []) or ready
    media, seen, ranges = [], set(), []
    for size in attrs.get('media-size-supported', []):
        x, y = first(size, 'x-dimension'), first(size, 'y-dimension')
        if isinstance(x, list) and isinstance(y, list) and len(x) == len(y) == 2 and all(dimension(v) for v in x + y):
            ranges.append({'width': x, 'height': y})
    collections = list(database)
    if not collections:
        # Size-only fallback is deliberately conservative about margins.
        for size in attrs.get('media-size-supported', []):
            if dimension(first(size, 'x-dimension')) and dimension(first(size, 'y-dimension')):
                record = {'media-size': [size]}
                for edge in ('left', 'right', 'top', 'bottom'):
                    supported = attrs.get('media-' + edge + '-margin-supported', [])
                    if supported:
                        record['media-' + edge + '-margin'] = [max(v for v in supported if isinstance(v, int))]
                collections.append(record)
    if not collections:
        for name in names:
            size = media_keyword_size(name)
            if size:
                collections.append({'media-size': [{'x-dimension': [size[0]], 'y-dimension': [size[1]]}]})
    for collection in collections:
        size = first(collection, 'media-size', {})
        width, height = first(size, 'x-dimension'), first(size, 'y-dimension')
        if not dimension(width) or not dimension(height):
            # A range is a custom-size envelope, not a ready-to-print preset.
            if isinstance(width, list) and isinstance(height, list) and len(width) == len(height) == 2 and all(dimension(v) for v in width + height):
                ranges.append({'width': width, 'height': height, 'margins': [first(collection, 'media-' + e + '-margin') for e in ('left', 'top', 'right', 'bottom')], 'media_type': first(collection, 'media-type'), 'media_source': first(collection, 'media-source')})
            continue
        margins = [first(collection, 'media-' + e + '-margin') for e in ('left', 'top', 'right', 'bottom')]
        known = all(isinstance(v, int) and 0 <= v < min(width, height) for v in margins)
        if not known:
            # Unknown margins cannot establish an exact printable area.
            margins = [0, 0, 0, 0]
        if margins[0] + margins[2] >= width or margins[1] + margins[3] >= height:
            continue
        name = first(collection, 'media-key') or next((n for n in names if media_keyword_size(n) == (width, height)), None)
        record = {'width': width, 'height': height, 'margins': margins,
                  'margins_known': known, 'media': name,
                  'media_type': first(collection, 'media-type'),
                  'media_source': first(collection, 'media-source')}
        signature = fingerprint(record)
        if signature in seen:
            continue
        seen.add(signature)
        record['id'] = signature[:20]
        record['label'] = f'{width / 100:g} × {height / 100:g} mm · ' + ('无边距' if known and not any(margins) else '有边距' if known else '边距未报告')
        record['ready'] = any(first(r.get('media-size', [{}])[0], 'x-dimension') == width and first(r.get('media-size', [{}])[0], 'y-dimension') == height for r in ready) or name in attrs.get('media-ready', [])
        media.append(record)
    media.sort(key=lambda m: (not m['ready'], abs(m['width'] - 10160) + abs(m['height'] - 15240), sum(m['margins']), m['id']))
    resolutions = []
    for r in attrs.get('printer-resolution-supported', []) or attrs.get('printer-resolution-default', []):
        if not isinstance(r, dict) or r.get('units') not in (3, 4):
            continue
        x, y = r.get('x', 0), r.get('y', 0)
        if not isinstance(x, int) or not isinstance(y, int) or not (36 <= x <= 4800 and 36 <= y <= 4800):
            continue
        scale = 2.54 if r['units'] == 4 else 1
        item = {'id': f'{x}x{y}-{r["units"]}', 'x': x, 'y': y, 'units': r['units'], 'dpi_x': x * scale, 'dpi_y': y * scale}
        item['label'] = f'{item["dpi_x"]:g} × {item["dpi_y"]:g} dpi'
        if item not in resolutions:
            resolutions.append(item)
    resolutions.sort(key=lambda r: (abs(r['dpi_x'] - 300) + abs(r['dpi_y'] - 300), r['id']))
    compatible = 'image/jpeg' in formats and bool(media) and bool(resolutions) and 2 in attrs.get('operations-supported', []) and 9 in attrs.get('operations-supported', []) and 4 in attrs.get('operations-supported', [])
    reasons = []
    if 'image/jpeg' not in formats:
        reasons.append('设备不接受 JPEG；PWG Raster/URF 或专用驱动设备暂需其他后端')
    if not media:
        reasons.append('未报告可用相纸尺寸')
    if not resolutions:
        reasons.append('未报告可用分辨率')
    if not all(op in attrs.get('operations-supported', []) for op in (2, 4, 9)):
        reasons.append('缺少打印、无出纸校验或任务状态查询操作')
    if 'none' not in attrs.get('print-scaling-supported', []):
        reasons.append('设备未报告关闭缩放的能力')
    if not any(m['margins_known'] for m in media):
        reasons.append('设备未报告完整相纸边距')
    compatible = compatible and not reasons
    return {'formats': formats, 'media': media, 'custom_ranges': ranges, 'resolutions': resolutions,
            'qualities': [q for q in attrs.get('print-quality-supported', []) if q in (3, 4, 5)],
            'scaling': attrs.get('print-scaling-supported', []), 'compatible': compatible, 'reasons': reasons,
            'jpeg_max_bytes': max(attrs.get('jpeg-k-octets-supported', [[0, 20480]])[0]) * 1024 if isinstance(first(attrs, 'jpeg-k-octets-supported'), list) else 20 * 1024 * 1024}


def make_ticket(device, media_id=None, resolution_id=None, custom_width_mm=None, custom_height_mm=None):
    caps = device['capabilities']
    if not caps.get('compatible'):
        raise ValueError('此设备尚不能使用通用 JPEG 打印：' + '；'.join(caps.get('reasons', [])))
    media = next((m for m in caps['media'] if m['id'] == media_id), None) if media_id else caps['media'][0]
    if media_id == 'custom':
        if custom_width_mm is None or custom_height_mm is None:
            raise ValueError('请填写自定义相纸宽高')
        w, h = round(custom_width_mm * 100), round(custom_height_mm * 100)
        if not any(r['width'][0] <= w <= r['width'][1] and r['height'][0] <= h <= r['height'][1] for r in caps['custom_ranges']):
            raise ValueError('自定义相纸尺寸超出设备报告的范围')
        matching = [r for r in caps['custom_ranges'] if r['width'][0] <= w <= r['width'][1] and r['height'][0] <= h <= r['height'][1] and all(isinstance(v, int) and v >= 0 for v in r.get('margins', [None]))]
        if not matching:
            raise ValueError('自定义相纸没有完整边距组合，请使用标准纸型')
        base = matching[0]
        media = dict(base, id='custom', width=w, height=h, media=None, margins_known=True, label=f'{w / 100:g} × {h / 100:g} mm · 自定义')
    if media is None:
        raise ValueError('相纸选项已变化，请刷新设备能力')
    if not media['margins_known']:
        raise ValueError('设备未报告这张相纸的边距，暂不能保证排版范围')
    if media['margins'][0] + media['margins'][2] >= media['width'] or media['margins'][1] + media['margins'][3] >= media['height']:
        raise ValueError('纸张边距超过可打印范围')
    resolution = next((r for r in caps['resolutions'] if r['id'] == resolution_id), None) if resolution_id else caps['resolutions'][0]
    if resolution is None:
        raise ValueError('分辨率选项已变化，请重新选择')
    if 'none' not in caps['scaling']:
        raise ValueError('设备未报告关闭缩放的能力，精确尺寸打印暂不可用')
    return {'printer_id': device['id'], 'printer_name': device['name'], 'printer_uri': device['uri'],
            'revision': device['revision'], 'capability_hash': device['capability_hash'],
            'media': media, 'resolution': resolution, 'quality': 5 if 5 in caps['qualities'] else 4 if 4 in caps['qualities'] else None,
            'scaling': 'none', 'format': 'image/jpeg', 'jpeg_max_bytes': caps.get('jpeg_max_bytes', 20 * 1024 * 1024)}


def page_geometry(ticket, layout, border_mm=0, gap_mm=2, photo_width_mm=None, photo_height_mm=None):
    m = ticket['media']
    w, h = m['width'] / 100, m['height'] / 100
    left, top, right, bottom = [v / 100 + border_mm for v in m['margins']]
    aw, ah = w - left - right, h - top - bottom
    if aw <= 0 or ah <= 0:
        raise ValueError('边距超过纸张可打印范围')
    if layout in ('custom', 'square3'):
        pw, ph = (76.2, 76.2) if layout == 'square3' else (photo_width_mm, photo_height_mm)
        if pw is None or ph is None or not (5 <= pw <= 1000 and 5 <= ph <= 1000):
            raise ValueError('请填写有效的照片宽高（至少 5 mm）')
        gap = 0 if layout == 'square3' else gap_mm
        cols = math.floor((aw + gap + 1e-7) / (pw + gap))
        rows = math.floor((ah + gap + 1e-7) / (ph + gap))
        if cols * rows < 1 or (layout == 'square3' and cols * rows < 2):
            raise ValueError('照片尺寸或双拼模板无法放入所选纸张的可打印范围')
        if cols * rows > 100:
            raise ValueError('单页最多 100 格，请增大照片尺寸')
        if layout == 'square3':
            cols, rows = 1, 2
        start_x = left + (aw - cols * pw - (cols - 1) * gap) / 2
        start_y = top + (ah - rows * ph - (rows - 1) * gap) / 2
    else:
        if layout not in ('single', 'duo', 'grid4', 'grid9'):
            raise ValueError('排版模板不正确')
        cols, rows = {'single': (1, 1), 'duo': (1, 2), 'grid4': (2, 2), 'grid9': (3, 3)}[layout]
        gap = gap_mm
        pw, ph = (aw - (cols - 1) * gap) / cols, (ah - (rows - 1) * gap) / rows
        start_x, start_y = left, top
        if pw <= 0 or ph <= 0:
            raise ValueError('照片间距超过可打印范围')
    dx, dy = ticket['resolution']['dpi_x'], ticket['resolution']['dpi_y']
    pixels = (round(w / 25.4 * dx), round(h / 25.4 * dy))
    if pixels[0] * pixels[1] > 40_000_000 or max(pixels) > 15000:
        raise ValueError('此纸张与分辨率生成的图片过大，请选择较低分辨率')
    slots = [(start_x + c * (pw + gap), start_y + r * (ph + gap), pw, ph) for r in range(rows) for c in range(cols)]
    return {'width_mm': w, 'height_mm': h, 'pixels': pixels, 'dpi': (dx, dy), 'slots': slots,
            'capacity': cols * rows, 'photo_mm': (pw, ph), 'margins_mm': (left, top, right, bottom)}
