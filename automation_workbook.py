"""Read the LoFi queue and update native checkbox values without reserializing Excel XML."""
import io
import os
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import posixpath
import re
import tempfile
import xml.etree.ElementTree as ET
from zipfile import ZipFile

NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}


@contextmanager
def workbook_lock(path):
    # OS locks are released on process exit. Keep the lock file so another
    # process cannot lock a different inode while the first one is in use.
    with path.with_name(path.name + '.lofi.lock').open('a+b') as handle:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(handle, fcntl.LOCK_UN)
        try:
            acquire()
        except OSError:
            raise ValueError('File Excel đang được một tác vụ khác cập nhật.') from None
        try:
            yield
        finally:
            release()


def worksheet_member(book, name):
    links = {item.attrib['Id']: item.attrib['Target']
             for item in ET.fromstring(book.read('xl/_rels/workbook.xml.rels'))}
    sheets = ET.fromstring(book.read('xl/workbook.xml')).findall('s:sheets/s:sheet', NS)
    sheet = next((sheet for sheet in sheets if sheet.attrib['name'] == name), None)
    if sheet is None:
        raise ValueError(f'Missing worksheet: {name}')
    target = links[sheet.attrib['{' + NS['r'] + '}id']]
    member = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
    if not member.startswith('xl/') or '..' in PurePosixPath(member).parts:
        raise ValueError('Invalid worksheet path')
    return member


def read_sheets(path: Path) -> dict:
    with ZipFile(path) as book:
        if sum(item.file_size for item in book.infolist()) > 80_000_000:
            raise ValueError('Workbook exceeds the 80 MB uncompressed limit')
        strings = []
        if 'xl/sharedStrings.xml' in book.namelist():
            strings = [''.join(item.itertext()) for item in ET.fromstring(book.read('xl/sharedStrings.xml'))]
        result = {}
        for sheet in ET.fromstring(book.read('xl/workbook.xml')).findall('s:sheets/s:sheet', NS):
            member = worksheet_member(book, sheet.attrib['name'])
            rows = {}
            for row in ET.fromstring(book.read(member)).findall('s:sheetData/s:row', NS):
                cells = {}
                for cell in row.findall('s:c', NS):
                    column = re.match(r'[A-Z]+', cell.attrib['r'])[0]
                    value = cell.findtext('s:v', '', NS)
                    kind = cell.attrib.get('t')
                    if kind == 's': value = strings[int(value)]
                    elif kind == 'inlineStr': value = ''.join(cell.find('s:is', NS).itertext())
                    elif kind == 'b': value = value == '1'
                    cells[column] = value
                rows[int(row.attrib['r'])] = cells
            result[sheet.attrib['name']] = rows
        return result


def queue_records(path: Path) -> list[dict]:
    sheets = read_sheets(path)
    required = {'Kich_ban', 'Prompts', 'YouTube'}
    if not required.issubset(sheets):
        raise ValueError('Workbook requires Kich_ban, Prompts and YouTube sheets')
    def indexed(name):
        result = {}
        for row, cells in sheets[name].items():
            key = str(cells.get('A', '')).strip()
            if re.fullmatch(r'LOFI-\d{3,}', key):
                if key in result: raise ValueError(f'Duplicate script ID in {name}: {key}')
                result[key] = (row, cells)
        return result
    prompts, youtube = indexed('Prompts'), indexed('YouTube')
    output = []
    for key, (row, cells) in indexed('Kich_ban').items():
        done = cells.get('I', False)
        if done is True or str(done).upper() in ('TRUE', '1'): continue
        if str(cells.get('H', '')).strip() == 'BO_QUA': continue
        if key not in prompts or key not in youtube: raise ValueError(f'Missing prompt or YouTube row: {key}')
        p, y = prompts[key][1], youtube[key][1]
        if any(not str(p.get(column, '')).strip() for column in ('B', 'D', 'E')):
            raise ValueError(f'Missing image/animation/music prompt: {key}')
        output.append({'id': key, 'row': row, 'checkbox_cell': f'I{row}', 'sheet': 'Kich_ban',
            'title': str(cells.get('B') or key), 'image_prompt_1': p['B'],
            'animation_prompt': p['D'], 'music_prompt': p['E'], 'track_title': p.get('F', key),
            'thumbnail_prompt': str(p.get('G') or '').strip(),
            'youtube': dict(zip(('title', 'description', 'tags', 'hashtags'), (y.get(c, '') for c in 'BCDE')))})
    if not output and not indexed('Kich_ban'):
        raise ValueError('No LOFI IDs found in workbook')
    return output


def mark_completed(path: Path, sheet: str, record_id: str, id_cell: str,
                   cell: str, backup: Path) -> None:
    """Verify the row and atomically set its boolean. Preserve every other ZIP member."""
    path = path.resolve()
    if not re.fullmatch(r'A[1-9]\d*', id_cell) or cell != 'I' + id_cell[1:]:
        raise ValueError('Invalid script checkbox address')
    if path.with_name('~$' + path.name).exists():
        raise ValueError('Đóng file Excel trên máy rồi bấm Tiếp tục để lưu ô hoàn thành.')
    temporary = None
    try:
        with workbook_lock(path):
            original = path.read_bytes()
            rows = read_sheets(io.BytesIO(original)).get(sheet, {})
            row = rows.get(int(id_cell[1:]), {})
            if row.get('A') != record_id:
                raise ValueError('Excel row ID does not match; reload the workbook before retrying.')
            with ZipFile(io.BytesIO(original)) as source:
                member = worksheet_member(source, sheet)
                xml = source.read(member)
                root = ET.fromstring(xml)
                matches = root.findall(f's:sheetData/s:row/s:c[@r="{cell}"]', NS)
                if len(matches) != 1:
                    raise ValueError('Checkbox cell is missing or duplicated')
                target = matches[0]
                value = target.findtext('s:v', '', NS)
                if target.find('s:f', NS) is not None or target.get('t', 'n') not in ('b', 'n') or value not in ('0', '1'):
                    raise ValueError('Checkbox must contain a literal boolean or 0/1')
                if value == '1':
                    return
                # Patch the original bytes so namespace prefixes, rich formatting,
                # featurePropertyBag and native checkbox metadata survive intact.
                pattern = rb'<(?:[\w.-]+:)?c\b[^>]*\br=["\x27]' + cell.encode() + rb'["\x27][^>]*>.*?</(?:[\w.-]+:)?c>'
                cells = list(re.finditer(pattern, xml, re.S))
                if len(cells) != 1:
                    raise ValueError('Cannot locate the checkbox XML safely')
                span = cells[0]
                changed, count = re.subn(rb'(<(?:[\w.-]+:)?v\b[^>]*>)0(</(?:[\w.-]+:)?v>)',
                                         rb'\g<1>1\g<2>', span.group(), count=1)
                if count != 1:
                    raise ValueError('Cannot locate the checkbox value safely')
                updated = xml[:span.start()] + changed + xml[span.end():]
                with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.xlsx', delete=False) as temp:
                    temporary = Path(temp.name)
                with ZipFile(temporary, 'w') as output:
                    output.comment = source.comment
                    for entry in source.infolist():
                        output.writestr(entry, updated if entry.filename == member else source.read(entry))
            verified = read_sheets(temporary)[sheet][int(id_cell[1:])]
            if verified['A'] != record_id or verified['I'] not in (True, '1'):
                raise ValueError('Saved checkbox could not be verified')
            if path.read_bytes() != original:
                raise ValueError('File Excel vừa thay đổi; bấm Tiếp tục để kiểm tra lại.')
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                with backup.open('xb') as saved:
                    saved.write(original)
            os.replace(temporary, path)
    except PermissionError:
        raise ValueError('Không ghi được file Excel. Đóng file trong Excel rồi bấm Tiếp tục.') from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
