import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image
from capabilities import normalize, make_ticket, page_geometry
from ipp import Printer
from imaging import render_page
import tempfile
import devices
import main


class GeometryTest(unittest.TestCase):
    def setUp(self):
        self.attrs = json.loads((Path(__file__).parent / 'fixtures/fuji-ipp.json').read_text())

    def device(self, attrs=None):
        return dict(id='one', name='Example', uri='ipp://192.168.1.10/ipp/print', revision=1, capability_hash='x', capabilities=normalize(attrs or self.attrs))

    def test_actual_media_combinations_and_custom_range(self):
        d = self.device()
        self.assertEqual(len(d['capabilities']['media']), 4)
        ticket = make_ticket(d)
        self.assertEqual(ticket['media']['margins'], [0]*4)
        custom = make_ticket(d, 'custom', custom_width_mm=90, custom_height_mm=130)
        self.assertEqual(custom['media']['width'], 9000)
        self.assertEqual(page_geometry(ticket, 'square3')['photo_mm'], (76.2,76.2))
        g = page_geometry(ticket, 'custom', photo_width_mm=35, photo_height_mm=45)
        self.assertEqual(g['capacity'], 6)
        self.assertEqual(g['photo_mm'], (35,45))
        with self.assertRaises(ValueError):
            make_ticket(d, 'custom', custom_width_mm=200, custom_height_mm=300)

    def test_missing_margins_cannot_be_invented_for_custom_size(self):
        self.attrs['media-col-database'][-1].pop('media-left-margin')
        with self.assertRaises(ValueError):
            make_ticket(self.device(), 'custom', custom_width_mm=90, custom_height_mm=130)

    def test_asymmetric_resolution_and_nonzero_margins(self):
        self.attrs['printer-resolution-supported'] = [{'x':300,'y':600,'units':3}]
        d = self.device()
        media = next(m for m in d['capabilities']['media'] if m['width']==10160 and any(m['margins']))
        g=page_geometry(make_ticket(d,media['id']),'custom',photo_width_mm=35,photo_height_mm=45)
        self.assertEqual(g['pixels'], (1200,3600))
        self.assertEqual(g['margins_mm'], (2.54,)*4)
        self.attrs['printer-resolution-supported'] = [{'x':118,'y':118,'units':4}]
        self.assertAlmostEqual(make_ticket(self.device())['resolution']['dpi_x'],299.72)

    def test_strict_validation_rejects_substitutions_and_unsupported(self):
        p=Printer('ipp://192.168.1.10/ipp/print')
        for response in ({'status':1,'groups':[], 'attributes':{}}, {'status':0,'groups':[(5,{'media':['x']})], 'attributes':{}}):
            with patch.object(p,'request',return_value=response), self.assertRaises(ValueError):
                p.validate_ticket(make_ticket(self.device()))

    def test_public_and_credential_targets_rejected(self):
        for uri in ('http://192.168.1.1/', 'ipp://user:pass@192.168.1.1/ipp', 'ipp://192.168.1.1:22/ipp'):
            with self.assertRaises(ValueError): Printer(uri,lan_only=True)
        for host in ('127.0.0.1','8.8.8.8','169.254.169.254','::1'):
            with self.assertRaises(ValueError): Printer('ipp://['+host+']/ipp' if ':' in host else 'ipp://'+host+'/ipp',lan_only=True).destination()

    def test_non_square_dpi_keeps_physical_square(self):
        self.attrs['printer-resolution-supported']=[{'x':300,'y':600,'units':3}]
        ticket=make_ticket(self.device())
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'square.jpg'
            Image.new('RGB',(500,500),'red').save(path)
            blob,_,_=render_page([{'path':path}],ticket,'custom',photo_width_mm=35,photo_height_mm=45)
            with Image.open(io.BytesIO(blob)) as image:
                box=image.getchannel('G').point(lambda p:255 if p<50 else 0).getbbox()
                width=(box[2]-box[0])/300*25.4
                height=(box[3]-box[1])/600*25.4
                self.assertAlmostEqual(width,height,delta=.2)
