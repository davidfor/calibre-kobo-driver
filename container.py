# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:ai

__license__ = 'GPL v3'
__copyright__ = '2010, Kovid Goyal <kovid@kovidgoyal.net>; 2013, Joel Goguen <jgoguen@jgoguen.ca>'
__docformat__ = 'restructuredtext en'

import os
import re
import shutil
import string
import sys
import time

from lxml import etree
from lxml.etree import XMLSyntaxError

from calibre import guess_type
from calibre import prepare_string_for_xml
from calibre.constants import DEBUG
from calibre.constants import iswindows
from calibre.ebooks.chardet import substitute_entites
from calibre.ebooks.chardet import xml_to_unicode
from calibre.ebooks.conversion.plugins.epub_input import ADOBE_OBFUSCATION
from calibre.ebooks.conversion.plugins.epub_input import IDPF_OBFUSCATION
from calibre.ebooks.conversion.utils import HeuristicProcessor
from calibre.ptempfile import PersistentTemporaryDirectory
from calibre.utils import logging
from calibre.utils import zipfile
from calibre.utils.smartypants import smartyPants

from calibre.ebooks.oeb.polish.container import OPF_NAMESPACES
from calibre.ebooks.oeb.polish.container import EpubContainer

from copy import deepcopy
from urllib import unquote

HTML_MIMETYPES = frozenset(['text/html', 'application/xhtml+xml'])
EXCLUDE_FROM_ZIP = frozenset(['mimetype', '.DS_Store', 'thumbs.db', '.directory'])
NO_SPACE_BEFORE_CHARS = frozenset([c for c in string.punctuation] + [u'\xbb'])
ENCRYPTION_NAMESPACES = {'enc': 'http://www.w3.org/2001/04/xmlenc#', 'deenc': 'http://ns.adobe.com/digitaleditions/enc'}
XHTML_NAMESPACE = 'http://www.w3.org/1999/xhtml'


class InvalidEpub(ValueError):
    pass


class ParseError(ValueError):
    def __init__(self, name, desc):
        self.name = name
        self.desc = desc
        ValueError.__init__(self, _('Failed to parse: {0} with error: {1}').format(name, desc))


class KEPubContainer(EpubContainer):
    paragraph_counter = 0
    segment_counter = 0

    def get_html_names(self):
        """A generator function that yields only HTML file names from
        the ePub.
        """
        for node in self.opf_xpath('//opf:manifest/opf:item[@href and @media-type]'):
            if node.get("media-type") in HTML_MIMETYPES:
                href = os.path.join(os.path.dirname(self.opf_name), node.get("href"))
                href = os.path.normpath(href).replace(os.sep, '/')
                yield href

    @property
    def is_drm_encumbered(self):
        """Determine if the ePub container is encumbered with Digital
        Restrictions Management.

        This method looks for the 'encryption.xml' file which denotes an
        ePub encumbered by Digital Restrictions Management. DRM-encumbered
        files cannot be edited.
        """
        is_encumbered = False
        if 'META-INF/encryption.xml' in self.name_path_map:
            try:
                xml = self.parsed('META-INF/encryption.xml')
                if xml is None:
                    return True  # If encryption.xml can't be parsed, assume its presence means an encumbered file
                for elem in xml.xpath('./enc:EncryptedData/enc:EncryptionMethod[@Algorithm]', namespaces=ENCRYPTION_NAMESPACES):
                    alg = elem.get('Algorithm')

                    # Anything not in acceptable_encryption_algorithms is a sign of an
                    # encumbered file.
                    if alg not in {ADOBE_OBFUSCATION, IDPF_OBFUSCATION}:
                        is_encumbered = True
            except Exception as e:
                self.log.error("Could not parse encryption.xml: " + e.message)
                raise

        return is_encumbered

    def fix_tail(self, item):
        '''
        Designed only to work with self closing elements after item has
        just been inserted/appended
        '''
        parent = item.getparent()
        idx = parent.index(item)
        if idx == 0:
            item.tail = parent.text
        else:
            item.tail = parent[idx - 1].tail
            if idx == len(parent) - 1:
                parent[idx - 1].tail = parent.text

    def copy_file_to_container(self, path, name=None, mt=None):
        '''Copy a file into this Container instance.

        @param path: The path to the file to copy into this Container.
        @param name: The name to give to the copied file, relative to the Container root. Set to None to use the basename of path.
        @param mt: The MIME type of the file to set in the manifest. Set to None to auto-detect.

        @return: The name of the file relative to the Container root
        '''
        if path is None or re.match(r'^\s*$', path, re.MULTILINE):
            raise ValueError("A source path must be given")
        if name is None:
            name = os.path.basename(path)
        self.log.info("Copying file '{0}' to '{1}'".format(path, self.root))
        shutil.copy(path, os.path.join(self.root, name))
        item = self.generate_item(name, media_type=mt)

        return item.get('href')

    def add_content_file_reference(self, name):
        '''Add a reference to the named file (from self.name_path_map) to all content files (self.get_html_names()). Currently
        only CSS files with a MIME type of text/css and JavaScript files with a MIME type of application/x-javascript are
        supported.
        '''
        if name not in self.name_path_map or name not in self.mime_map:
            raise ValueError("A valid file name must be given (got: {0})".format(name))
        for file in self.get_html_names():
            self.log.info("Adding reference to {0} to file {1}".format(name, file))
            root = self.parsed(file)
            if root is None:
                self.log.error("Could not retrieve content file {0}".format(file))
                continue
            head = root.xpath('./xhtml:head', namespaces={'xhtml': XHTML_NAMESPACE})
            if head is None:
                self.log.error("Could not find a <head> element in content file {0}".format(file))
                continue
            head = head[0]
            if head is None:
                self.log.error("A <head> section was found but was undefined in content file {0}".format(file))
                continue

            if self.mime_map[name] == guess_type('a.css')[0]:
                elem = head.makeelement("{%s}link" % XHTML_NAMESPACE, rel='stylesheet', href=os.path.relpath(name, os.path.dirname(file)).replace(os.sep, '/'))
            elif self.mime_map[name] == guess_type('a.js')[0]:
                elem = head.makeelement("{%s}script" % XHTML_NAMESPACE, type='text/javascript', src=os.path.relpath(name, os.path.dirname(file)).replace(os.sep, '/'))
            else:
                elem = None

            if elem is not None:
                head.append(elem)
                if self.mime_map[name] == guess_type('a.css')[0]:
                    self.fix_tail(elem)
                self.dirty(file)

    def get_raw(self, name):
        self.commit_item(name, keep_parsed=False)
        f = open(self.name_path_map[name], 'rb')
        data = f.read()
        f.close()
        return data

    def flush_cache(self):
        for name in [n for n in self.dirtied]:
            self.commit_item(name, keep_parsed=True)

    def __hyphenate_node(self, elem, hyphenator, hyphen=u'\u00AD'):
        if elem is None:
            return None

        if isinstance(elem, basestring):
            newstr = []
            for w in elem.split():
                if len(w) > 3 and '-' not in w and hyphen not in w:
                    w = hyphenator.inserted(w, hyphen=hyphen)
                newstr.append(w)
            elem = " ".join(newstr)
        else:
            if elem.text is None and elem.tail is None:
                # If we get here, there's only child nodes
                for node in elem.xpath('./node()'):
                    node = self.__hyphenate_node(node, hyphenator, hyphen)
            else:
                elem.text = self.__hyphenate_node(elem.text, hyphenator, hyphen)
                if elem.text is not None:
                    elem.text += u" "
                elem.tail = self.__hyphenate_node(elem.tail, hyphenator, hyphen)
        return elem

    def hyphenate(self, hyphenator, hyphen=u'\u00AD'):
        if hyphenator is None or hyphen is None or hyphen == '':
            return False
        for name in self.get_html_names():
            self.log.info("Hyphenating file {0}".format(name))
            root = self.parsed(name)
            for node in root.xpath("./xhtml:body//xhtml:span[starts-with(@id, 'kobo.')]", namespaces={'xhtml': XHTML_NAMESPACE}):
                node = self.__hyphenate_node(node, hyphenator, hyphen)
            self.dirty(name)
        return True

    def __add_kobo_spans_to_node(self, node):
        if node is None or isinstance(node, etree._Comment):
            return None
        # Don't munge Processing Instruction nodes
        if isinstance(node, etree._ProcessingInstruction):
            if node.tail is not None:
                node.tail = None
            return node

        if isinstance(node, basestring):
            self.segment_counter += 1
            groups = re.split(ur'(.*?[\.\!\?\:][\'"\u201d\u2019]?\s*)', node, flags=re.UNICODE | re.MULTILINE)
            groups = [g.decode("utf-8") for g in groups if not re.match(r'^\s*$', g.strip(), re.UNICODE | re.MULTILINE)]

            # HACK: Account for nodes that have a whitespace-only text node
            if len(groups) == 0 and re.match(ur'^\s+$', node, flags=re.UNICODE | re.MULTILINE):
                return node

            ngroups = len(groups)
            if ngroups > 0:
                cur_group = 0
                text_container = etree.Element("{%s}span" % (XHTML_NAMESPACE,), attrib={"id": "kobo.{0}.{1}".format(self.paragraph_counter, self.segment_counter), "class": "koboSpan"})
                for g in groups:
                    cur_group += 1
                    if text_container.text is None:
                        text_container.text = g
                    elif cur_group < ngroups:
                        self.segment_counter += 1
                        span = etree.Element("{%s}span" % (XHTML_NAMESPACE,), attrib={"id": "kobo.{0}.{1}".format(self.paragraph_counter, self.segment_counter), "class": "koboSpan"})
                        span.text = g
                        text_container.append(span)
                    else:
                        text_children = text_container.getchildren()
                        if len(text_children) > 0 and text_children[-1] is not None:
                            text_children[-1].tail = g
                        else:
                            if re.match(ur'^\s+$', g, flags=re.UNICODE | re.MULTILINE) or g[0] in NO_SPACE_BEFORE_CHARS:
                                text_container.text += g
                            else:
                                text_container.text += " " + g
                return text_container
            return None
        else:
            # First process the text
            newtext = None
            if node.text is not None:
                newtext = self.__add_kobo_spans_to_node(node.text)

            # Clone the rest of the node, clear the node, and add the text node
            children = deepcopy(node.getchildren())
            nodeattrs = {}
            for key in node.keys():
                nodeattrs[key] = node.get(key)
            node.clear()
            for key in nodeattrs.keys():
                node.set(key, nodeattrs[key])
            if newtext is not None:
                if isinstance(newtext, basestring):
                    node.text = newtext
                else:
                    node.append(newtext)

            # For each child, process the child and then process and append its tail
            for elem in children:
                elemtail = deepcopy(elem.tail) if elem.tail is not None else None
                newelem = self.__add_kobo_spans_to_node(elem)
                if newelem is not None:
                    node.append(newelem)

                newtail = None
                if elemtail is not None:
                    newtail = self.__add_kobo_spans_to_node(elemtail)
                    if newtail is not None:
                        if isinstance(newtail, basestring):
                            node_children = node.getchildren()
                            if len(node_children) > 0 and node_children[-1] is not None:
                                node_children[-1].tail = newtail
                            else:
                                if re.match(ur'^\s+$', newtail, flags=re.UNICODE | re.MULTILINE):
                                    if node.text is not None:
                                        node.text += newtail
                                    else:
                                        node.text = newtail
                                else:
                                    if node.text is not None:
                                        node.text += u" " + newtail
                                    else:
                                        node.text = newtail
                        else:
                            node.append(newtail)

                self.paragraph_counter += 1
                self.segment_counter = 1
            return node
        return None

    def add_kobo_spans(self):
        for name in self.get_html_names():
            self.log.info("Adding Kobo spans to {0}".format(name))
            root = self.parsed(name)
            if len(root.xpath('.//xhtml:span[@class="koboSpan" or starts-with(@id, "kobo.")]', namespaces={'xhtml': XHTML_NAMESPACE})) > 0:
                self.log.info("\tSkipping file")
                continue
            self.paragraph_counter = 1
            self.segment_counter = 0
            body = root.xpath('./xhtml:body', namespaces={'xhtml': XHTML_NAMESPACE})[0]
            body = self.__add_kobo_spans_to_node(body)
            root = etree.tostring(root, pretty_print=True)
            # Re-open self-closing paragraph tags
            root = re.sub(r'<p[^>/]*/>', '<p></p>', root)
            self.dirty(name)
        self.flush_cache()
        return True

    def smarten_punctuation(self):
        preprocessor = HeuristicProcessor(log=self.log)

        for name in self.get_html_names():
            self.log.info("Smartening punctuation for file {0}".format(name))
            html = self.get_raw(name)
            html = html.encode("UTF-8")

            # Fix non-breaking space indents
            html = preprocessor.fix_nbsp_indents(html)
            # Smarten punctuation
            html = smartyPants(html)
            # Ellipsis to HTML entity
            html = re.sub(ur'(?u)(?<=\w)\s?(\.\s+?){2}\.', '&hellip;', html, flags=re.UNICODE | re.MULTILINE)
            # Double-dash and unicode char code to em-dash
            html = string.replace(html, '---', ' &#x2013; ')
            html = string.replace(html, u"\x97", ' &#x2013; ')
            html = string.replace(html, '--', ' &#x2014; ')
            html = string.replace(html, u"\u2014", ' &#x2014; ')
            html = string.replace(html, u"\u2013", ' &#x2013; ')

            # Fix comment nodes that got mangled
            html = string.replace(html, u'<! &#x2014; ', u'<!-- ')
            html = string.replace(html, u' &#x2014; >', u' -->')

            # Remove Unicode replacement characters
            html = string.replace(html, u"\uFFFD", "")

            self.dirty(name)
        self.flush_cache()

    def clean_markup(self):
        for name in self.get_html_names():
            self.log.info("Cleaning markup for file {0}".format(name))
            html = self.get_raw(name)
            html = html.encode("UTF-8")

            # Replace unicode dashes with ASCII representations - smarten punctuation picks this up if asked for
            html = string.replace(html, u"\u2014", ' -- ')
            html = string.replace(html, u"\u2013", ' --- ')
            html = string.replace(html, u"\x97", ' --- ')

            # Get rid of Microsoft cruft
            html = re.sub(ur'\s*<o:p>\s*</o:p>', ' ', html, flags=re.UNICODE | re.MULTILINE)
            html = re.sub(r'(?i)</?st1:\w+>', '', html, flags=re.UNICODE | re.MULTILINE)

            # Remove empty headings
            html = re.sub(r'(?i)<h\d+>\s*</h\d+>', '', html, flags=re.UNICODE | re.MULTILINE)

            # Remove Unicode replacement characters
            html = string.replace(html, u"\uFFFD", "")

            self.dirty(name)
        self.flush_cache()

    def forced_cleanup(self):
        for name in self.get_html_names():
            self.log.info("Forcing cleanup for file {0}".format(name))
            html = self.get_raw(name)
            html = html.encode("UTF-8")

            # Force meta and link tags to be self-closing
            html = re.sub(ur'<(meta|link) ([^>]+)></(?:meta|link)>', ur'<\1 \2 />', html, flags=re.UNICODE | re.MULTILINE)

            if name == 'index_split_000.xhtml':
                self.log.info("HTML after meta/link replacement:\n{0}".format(html))

            # Force open script tags
            html = re.sub(ur'<script (.+) ?/>', ur'<script \1></script>', html, flags=re.UNICODE | re.MULTILINE)

            # Force open self-closing paragraph tags
            html = re.sub(ur'<p[^>/]*/>', ur'<p></p>', html, flags=re.UNICODE | re.MULTILINE)

            # Force open self-closing script tags
            html = re.sub(ur'<script (.+) ?/>', ur'<script \1></script>', html, flags=re.UNICODE | re.MULTILINE)

            self.dirty(name)
        self.flush_cache()
