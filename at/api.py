from os import mkdir, path
from subprocess import run as proc_run, CalledProcessError
from uuid import uuid4

from flask import (
        Blueprint, current_app as app, jsonify, request, send_from_directory)
from lxml.etree import XMLSyntaxError
from werkzeug.utils import secure_filename
from xml2rfc.writers.base import default_options
from xml2rfc import (
        HtmlWriter, PdfWriter, PrepToolWriter, TextWriter, V2v3XmlWriter,
        XmlRfcParser)
from xml2rfc.writers.base import RfcWriterError

ALLOWED_EXTENSIONS = {'txt', 'xml', 'md', 'mkd'}
DIR_MODE = 0o770
BAD_REQUEST = 400
METADATA_JS_URL = 'https://www.rfc-editor.org/js/metadata.min.js'

bp = Blueprint('api', __name__, url_prefix='/api')


# Exceptions
class KramdownError(Exception):
    '''Error class for kramdown-rfc2629 errors'''
    pass


class TextError(Exception):
    '''Error class for id2xml errors'''
    pass


class XML2RFCError(Exception):
    '''Error class for xml2rfc errors'''
    pass


def allowed_file(filename):
    '''Return true if file extension in allowed list'''

    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_filename(filename, ext):
    '''Returns filename with given extension'''

    root, _ = path.splitext(filename)
    return '.'.join([root, ext])


def get_file(filename):
    '''Returns the filename part from a file path'''

    return filename.split('/')[-1]


def process_file(file):
    '''Returns XML version of the given file.
    NOTE: if file is an XML file, that file wouldn't go through conversion.'''

    dir_path = path.join(
            app.config['UPLOAD_DIR'],
            str(uuid4()))
    mkdir(dir_path, mode=DIR_MODE)

    filename = path.join(
            dir_path,
            secure_filename(file.filename))
    file.save(filename)

    app.logger.info('file saved at {}'.format(filename))

    _, file_ext = path.splitext(filename)

    if file_ext.lower() in ['.md', '.mkd']:
        filename = md2xml(filename)
    elif file_ext.lower() == '.txt':
        filename = txt2xml(filename)

    return (dir_path, filename)


def md2xml(filename):
    '''Convert kramdown-rfc2629 markdown file to XML'''

    app.logger.debug('processing kramdown-rfc2629 file')

    output = proc_run(
                args=['kramdown-rfc2629', filename],
                capture_output=True)

    try:
        output.check_returncode()
    except CalledProcessError as e:
        app.logger.info('kramdown-rfc2629 error: {}'.format(
            output.stderr.decode('utf-8')))
        raise KramdownError(output.stderr.decode('utf-8'))

    # write output to XML file
    xml_file = get_filename(filename, 'xml')
    with open(xml_file, 'wb') as file:
        file.write(output.stdout)

    app.logger.info('new file saved at {}'.format(filename))
    return xml_file


def txt2xml(filename):
    '''Convert text RFC file to XML'''

    app.logger.debug('processing text RFC file')

    xml_file = get_filename(filename, 'xml')

    output = proc_run(
                args=['id2xml', '--v3', '--out', xml_file, filename],
                capture_output=True)

    try:
        output.check_returncode()
    except CalledProcessError as e:
        app.logger.info('id2xml error: {}'.format(
            output.stderr.decode('utf-8')))
        raise TextError(output.stderr.decode('utf-8'))

    app.logger.info('new file saved at {}'.format(filename))
    return xml_file


def get_xml(filename):
    '''Convert/parse XML to XML2RFC v3
    NOTE: if file is XML2RFC v2 that will get converted to v3'''

    try:
        app.logger.debug('invoking xml2rfc parser')

        parser = XmlRfcParser(filename, quiet=True)
        xmltree = parser.parse(remove_comments=False, quiet=True)
        xmlroot = xmltree.getroot()
        xml2rfc_version = xmlroot.get('version', '2')

        # v2v3 conversion for v2 XML
        if xml2rfc_version == '2':
            app.logger.debug('converting v2 XML to v3 XML')

            v2v3 = V2v3XmlWriter(xmltree)
            xmltree.tree = v2v3.convert2to3()
            xmlroot = xmltree.getroot()
            v2v3.write(filename)
    except XMLSyntaxError as e:
        app.logger.info('xml2rfc error: {}'.format(str(e)))
        raise XML2RFCError(e)

    app.logger.info('new file saved at {}'.format(filename))
    return filename


def prep_xml(filename):
    '''Prepare XML file with xml2rfc'''

    try:
        parser = XmlRfcParser(filename, quiet=True)
        xmltree = parser.parse(remove_comments=False, quiet=True)

        # run prep tool
        app.logger.debug('running xml2rfc prep tool')
        prep = PrepToolWriter(xmltree, quiet=True, liberal=True)
        prep.options.accept_prepped = True
        xmltree.tree = prep.prep()
    except RfcWriterError as e:
        app.logger.error('xml2rfc preptool error: {}'.format(str(e)))
        raise XML2RFCError(e)

    if xmltree.tree is None:
        raise XML2RFCError(prep.errors)

    return xmltree


def get_html(filename):
    '''Render HTML'''

    xmltree = prep_xml(filename)

    # Update default options
    options = default_options
    options.metadata_js_url = METADATA_JS_URL

    # render html
    app.logger.debug('running xml2rfc html writer')
    html = HtmlWriter(xmltree, options=options, quiet=True)
    html_file = get_filename(filename, 'html')
    html.write(html_file)

    app.logger.info('new file saved at {}'.format(html_file))
    return html_file


def get_text(filename):
    '''Render text'''

    xmltree = prep_xml(filename)

    # render text
    app.logger.debug('running xml2rfc text writer')
    text = TextWriter(xmltree, quiet=True)
    text_file = get_filename(filename, 'txt')
    text.write(text_file)

    app.logger.info('new file saved at {}'.format(text_file))
    return text_file


def get_pdf(filename):
    '''Render PDF'''

    xmltree = prep_xml(filename)

    # render pdf
    app.logger.debug('running xml2rfc pdf writer')
    pdf = PdfWriter(xmltree, quiet=True)
    pdf_file = get_filename(filename, 'pdf')
    pdf.write(pdf_file)

    app.logger.info('new file saved at {}'.format(pdf_file))
    return pdf_file


@bp.route('/render/<format>', methods=('POST',))
def render(format):
    '''POST: /render/<format> API call
    Returns rendered format of the given input file.
    Returns JSON on event of an error.'''

    if 'file' not in request.files:
        app.logger.info('no input file')
        return jsonify(error='No file'), BAD_REQUEST

    file = request.files['file']

    if file.filename == '':
        app.logger.info('file name missing')
        return jsonify(error='Filename missing'), BAD_REQUEST

    if file and allowed_file(file.filename):
        try:
            dir_path, filename = process_file(file)
        except KramdownError as e:
            return jsonify(
                    error='kramdown-rfc2629 error: {}'.format(e)), BAD_REQUEST
        except TextError as e:
            return jsonify(error='id2xml error: {}'.format(e)), BAD_REQUEST

        try:
            xml_file = get_xml(filename)
        except XML2RFCError as e:
            return jsonify(error='xml2rfc error: {}'.format(e)), BAD_REQUEST

        rendered_filename = ''

        try:
            if format == 'xml':
                rendered_filename = get_file(xml_file)
            elif format == 'html':
                html_file = get_html(xml_file)
                rendered_filename = get_file(html_file)
            elif format == 'text':
                text_file = get_text(xml_file)
                rendered_filename = get_file(text_file)
            elif format == 'pdf':
                pdf_file = get_pdf(xml_file)
                rendered_filename = get_file(pdf_file)
            else:
                app.logger.info(
                        'render format not supported: {}'.format(format))
                return jsonify(
                        error='render format not supported'), BAD_REQUEST
        except XML2RFCError as e:
            return jsonify(error='xml2rfc error: {}'.format(e)), BAD_REQUEST

        return send_from_directory(
                dir_path,
                get_file(rendered_filename),
                as_attachment=True)
    else:
        app.logger.info('File format not supportted: {}'.format(file.filename))
        return jsonify(error='input file format not supported'), BAD_REQUEST
