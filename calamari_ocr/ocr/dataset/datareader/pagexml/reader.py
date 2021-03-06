import os
from random import shuffle

import numpy as np
from tfaip.base.data.pipeline.definitions import PipelineMode, INPUT_PROCESSOR, TARGETS_PROCESSOR
from tqdm import tqdm
from lxml import etree
import cv2 as cv
from typing import List, Generator
from enum import IntEnum
from calamari_ocr.ocr.dataset.params import InputSample, SampleMeta
from calamari_ocr.ocr.dataset.datareader.base import DataReader
from calamari_ocr.ocr.dataset.datareader.factory import FileDataReaderArgs
from calamari_ocr.utils import split_all_ext, filename

import logging

from calamari_ocr.utils.image import load_image

logger = logging.getLogger(__name__)


def xml_attr(elem, ns, label, default=None):
    try:
        return elem.xpath(label, namespaces=ns).pop()
    except IndexError as e:
        if default is None:
            raise e

        return default


class CutMode(IntEnum):
    BOX = 0
    POLYGON = 1
    MBR = 2


class PageXMLDatasetLoader:
    def __init__(self, mode: PipelineMode, non_existing_as_empty: bool, text_index: int, skip_invalid: bool=True):
        self.mode = mode
        self._non_existing_as_empty = non_existing_as_empty
        self.root = None
        self.text_index = text_index
        self.skip_invalid = skip_invalid

    def load(self, img, xml, skip_commented=True):
        if not os.path.exists(xml):
            if self._non_existing_as_empty:
                return None
            else:
                raise Exception("File '{}' does not exist.".format(xml))

        root = etree.parse(xml).getroot()
        self.root = root

        if self.mode in TARGETS_PROCESSOR:
            return self._samples_gt_from_book(root, img, xml, skip_commented)
        else:
            return self._samples_from_book(root, img, xml)

    def _samples_gt_from_book(self, root, img, page_id,
                              skipcommented=True):
        ns = {"ns": root.nsmap[None]}
        imgfile = root.xpath('//ns:Page',
                             namespaces=ns)[0].attrib["imageFilename"]
        if (self.mode in {PipelineMode.Training, PipelineMode.Evaluation}) and not split_all_ext(img)[0].endswith(split_all_ext(imgfile)[0]):
            raise Exception("Mapping of image file to xml file invalid: {} vs {} (comparing basename {} vs {})".format(
                img, imgfile, split_all_ext(img)[0], split_all_ext(imgfile)[0]))

        img_w = int(root.xpath('//ns:Page',
                               namespaces=ns)[0].attrib["imageWidth"])
        textlines = root.xpath('//ns:TextLine', namespaces=ns)

        for textline in textlines:
            tequivs = textline.xpath('./ns:TextEquiv[@index="{}"]'.format(self.text_index),
                                     namespaces=ns)

            if not tequivs:
                tequivs = textline.xpath('./ns:TextEquiv[not(@index)]', namespaces=ns)

            if len(tequivs) > 1:
                logger.warning("PageXML is invalid: TextLine includes TextEquivs with non unique ids")

            parat = textline.attrib
            if skipcommented and "comments" in parat and parat["comments"]:
                continue

            if tequivs is not None and len(tequivs) > 0:
                l = tequivs[0]
                text = l.xpath('./ns:Unicode', namespaces=ns).pop().text
                if text is None:
                    # Handle empty tag as empty string not as "not existing"
                    text = ''
            else:
                l = None
                text = None

            if text is None:
                if self.skip_invalid:
                    continue
                elif self._non_existing_as_empty:
                    text = ""
                else:
                    raise Exception("Empty text field")

            try:
                orientation = float(textline.xpath('../@orientation', namespaces=ns).pop())
            except (ValueError, IndexError):
                orientation = 0

            if self.mode in {PipelineMode.Training, PipelineMode.Evaluation}:
                if len(text) == 0:
                    # Empty lines cannot be used for training (CTC-loss can not be computed)
                    continue

            yield {
                'page_id': page_id,
                'ns': ns,
                "rtype": xml_attr(textline, ns, '../@type', ''),
                'xml_element': l,
                "image_path": img,
                "id": "{}/{}".format(page_id, xml_attr(textline, ns, './@id')),
                "text": text,
                "coords": xml_attr(textline, ns, './ns:Coords/@points'),
                "orientation": orientation,
                "img_width": img_w
            }

    def _samples_from_book(self, root, img, page_id):
        ns = {"ns": root.nsmap[None]}
        imgfile = root.xpath('//ns:Page',
                             namespaces=ns)[0].attrib["imageFilename"]
        if not split_all_ext(img)[0].endswith(split_all_ext(imgfile)[0]):
            raise Exception("Mapping of image file to xml file invalid: {} vs {} (comparing basename {} vs {})".format(
                img, imgfile, split_all_ext(img)[0], split_all_ext(imgfile)[0]))

        img_w = int(root.xpath('//ns:Page',
                               namespaces=ns)[0].attrib["imageWidth"])
        for l in root.xpath('//ns:TextLine', namespaces=ns):
            try:
                orientation = float(l.xpath('../@orientation', namespaces=ns).pop())
            except (ValueError, IndexError):
                orientation = 0

            yield {
                'page_id': page_id,
                'ns': ns,
                "rtype": xml_attr(l, ns, '../@type', ''),
                'xml_element': l,
                "image_path": img,
                "id": "{}/{}".format(page_id, xml_attr(l, ns, './@id')),
                "coords": xml_attr(l, ns, './ns:Coords/@points'),
                "orientation": orientation,
                "img_width": img_w,
                "text": None,
            }


class PageXMLReader(DataReader):
    def __init__(self,
                 mode: PipelineMode,
                 files,
                 xmlfiles: List[str] = None,
                 skip_invalid=False,
                 remove_invalid=True,
                 non_existing_as_empty=False,
                 args: FileDataReaderArgs = None,
                 ):
        """ Create a dataset from a Path as String

        Parameters
         ----------
        files : [], required
            image files
        skip_invalid : bool, optional
            skip invalid files
        remove_invalid : bool, optional
            remove invalid files
        """
        super().__init__(
            mode,
            skip_invalid, remove_invalid,
        )

        if xmlfiles is None:
            xmlfiles = []

        if args is None:
            args = {}

        self.args = args

        self.text_index = args.text_index

        self._non_existing_as_empty = non_existing_as_empty
        if len(xmlfiles) == 0:
            xmlfiles = [split_all_ext(p)[0] + ".xml" for p in files]

        if len(files) == 0:
            files = [None] * len(xmlfiles)

        self.files = files
        self.xmlfiles = xmlfiles
        self.pages = []
        for img, xml in zip(files, xmlfiles):
            loader = PageXMLDatasetLoader(self.mode, self._non_existing_as_empty, self.text_index, self.skip_invalid)
            for sample in loader.load(img, xml):
                self.add_sample(sample)

            self.pages.append(loader.root)

        # store which pagexml was stored last, to check when a file is ready to be written during sequential prediction
        self._last_page_id = None

    @staticmethod
    def cutout(pageimg: np.array, coordstring: str, mode: CutMode=CutMode.POLYGON, angle=0, cval=None, scale=1):
        """ Cut region from image
        Parameters
        ----------
        pageimg : page image
        coordstring : coordinates from PAGE in the form "c1_1,c_2 c2_1,c2_2 ..."
        mode :
            CutMode.BOX : cut straight rectangle around coordinates
            CutMode.POLYGON : cut polygon around coordinates
            CutMode.MBR : cut minimum bounding rectangle around coordinates
        angle :
            float : rotate angle in clockwise direction
            None : calculate angle from minimum bounding rectangle
        cval :
            colour : mask and fill empty regions with
            None : calculate via maximum pixel
        scale : factor to scale the coordinates with
        """

        coords = [p.split(",") for p in coordstring.split()]
        coords = [(int(scale*int(c[1])), int(scale*int(c[0]))) for c in coords]
        coords = np.array(coords, np.int32).reshape((-1, 1, 2))
        maxX, maxY = np.amax(coords, 0).squeeze()
        minX, minY = np.amin(coords, 0).squeeze()
        cut = pageimg[minX:maxX+1, minY:maxY+1]
        if cut.size == 0:
            return cut  # empty image
        coords -= (minX, minY)
        maxX, maxY = (maxX-minX, maxY-minY)
        minX, minY = (0, 0)

        # calculate angle if needed
        if angle is None:
            mbr = cv.minAreaRect(coords)
            angle = mbr[2] if maxX <= maxY else mbr[2] - 90

        # set cval if needed
        if cval is None:
            if cut.ndim == 2:
                cval = np.amax(cut).item()
            else:
                x, y = np.unravel_index(np.argmax(np.mean(cut, axis=2)), cut.shape[:2])
                cval = cut[x, y, :].tolist()

        # rotate cut
        if angle:
            (h, w) = cut.shape[:2]
            (cX, cY) = (w // 2, h // 2)
            M = cv.getRotationMatrix2D((cX, cY), -angle, 1.0)
            cos = np.abs(M[0, 0])
            sin = np.abs(M[0, 1])
            # compute the new bounding dimensions of the image
            nW = np.ceil((h * sin) + (w * cos)).astype(int)
            nH = np.ceil((h * cos) + (w * sin)).astype(int)
            # adjust the rotation matrix to take into account translation
            M[0, 2] += (nW / 2) - cX
            M[1, 2] += (nH / 2) - cY
            # rotate coords
            coords = cv.transform(coords[..., ::-1], M)
            minX, minY = np.amin(coords, 0).squeeze()
            maxX, maxY = np.amax(coords, 0).squeeze()
            # rotate image
            cut = cv.warpAffine(cut, M, (nW, nH), flags=cv.INTER_LINEAR,
                                borderMode=cv.BORDER_CONSTANT, borderValue=cval)
        else:
            coords = coords[..., ::-1]
            minX, minY = minY, minX
            maxX, maxY = maxY, maxX

        # simplify coordinates with MBR
        if mode is CutMode.MBR:
            mbr = cv.minAreaRect(coords)
            coords = cv.boxPoints(mbr).astype(int).reshape(-1, 1, 2)

        # mask pixels outside coords
        if mode in (CutMode.POLYGON, CutMode.MBR):
            box = (np.ones(cut.shape) * cval).astype(cut.dtype)
            mask = np.zeros(cut.shape, dtype=np.uint8)
            mask = cv.fillPoly(mask, [coords], color=[255] * cut.ndim)
            mask_inv = cv.bitwise_not(mask)
            fg = cv.bitwise_and(cut, mask)
            bg = cv.bitwise_and(box, mask_inv)
            cut = cv.add(fg, bg)

        return cut[minY:maxY+1, minX:maxX+1]

    def prepare_store(self):
        self._last_page_id = None

    def store_text(self, sentence, sample, output_dir, extension):
        ns = sample['ns']
        line = sample['xml_element']
        textequivxml = line.find('./ns:TextEquiv[@index="{}"]'.format(self.text_index),
                                 namespaces=ns)
        if textequivxml is None:
            textequivxml = etree.SubElement(line, "TextEquiv", attrib={"index": str(self.text_index)})

        u_xml = textequivxml.find('./ns:Unicode', namespaces=ns)
        if u_xml is None:
            u_xml = etree.SubElement(textequivxml, "Unicode")

        u_xml.text = sentence

        # check if page can be stored, this requires that (standard in prediction) the pages are passed sequentially
        if self._last_page_id != sample['page_id']:
            if self._last_page_id:
                self._store_page(extension, self._last_page_id)
            self._last_page_id = sample['page_id']

    def store_extended_prediction(self, data, sample, output_dir, extension):
        output_dir = os.path.join(output_dir, filename(sample['image_path']))
        if not os.path.exists(output_dir):
            os.mkdir(output_dir)

        super().store_extended_prediction(data, sample, output_dir, extension)

    def store(self, extension):
        if self._last_page_id:
            self._store_page(extension, self._last_page_id)
            self._last_page_id = None
        else:
            for xml, page in tqdm(zip(self.xmlfiles, self.pages), desc="Writing PageXML files", total=len(self.xmlfiles)):
                with open(split_all_ext(xml)[0] + extension, 'w') as f:
                    f.write(etree.tounicode(page.getroottree()))

    def _store_page(self, extension, page_id):
        page = self.pages[self.xmlfiles.index(page_id)]
        with open(split_all_ext(page_id)[0] + extension, 'w') as f:
            f.write(etree.tounicode(page.getroottree()))

    def _sample_iterator(self):
        all_samples = zip(self.files, self.xmlfiles, range(len(self.files)))
        if self.mode == PipelineMode.Training:
            all_samples = list(all_samples)
            shuffle(all_samples)
        return all_samples

    def _load_sample(self, sample, text_only) -> Generator[InputSample, None, None]:
        loader = PageXMLDatasetLoader(self.mode, self._non_existing_as_empty, self.text_index, self.skip_invalid)
        image_path, xml_path, idx = sample

        img = None
        if self.mode in INPUT_PROCESSOR:
            img = load_image(image_path)

        for i, sample in enumerate(loader.load(image_path, xml_path)):
            fold_id = (idx + i) % self.n_folds if self.n_folds > 0 else -1
            text = sample["text"]
            orientation = sample["orientation"]

            if not text_only and self.mode in INPUT_PROCESSOR:
                ly, lx = img.shape[:2]

                # rotate by orientation angle in clockwise direction to correct present skew
                angle = orientation if orientation and orientation % 360 != 0 else 0

                line_img = PageXMLReader.cutout(img, sample['coords'],
                                                mode=CutMode.POLYGON,
                                                angle=angle,
                                                cval=None,
                                                scale=lx / sample['img_width'])

                # add padding as required from normal files
                if self.args.pad:
                    pad = self.args.pad
                    img = np.pad(img, pad, mode='constant', constant_values=img.max())
            else:
                line_img = None

            yield InputSample(line_img, text, SampleMeta(id=sample['id'], fold_id=fold_id))
