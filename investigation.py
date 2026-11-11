"""Theo dõi bằng chứng thực sự đã đọc; không chứa đáp án hay ID của eval."""
import re
import unicodedata
from urllib.parse import urlsplit

from bonus.tools import HOSTS


def fold(text):
    text = unicodedata.normalize('NFD', str(text).casefold()).replace('đ', 'd')
    return ''.join(c for c in text if not unicodedata.combining(c))


class Investigation:
    def __init__(self, question, schemas, mode='snapshot'):
        text = fold(question)
        self.follow_links = any(word in text for word in (
            'theo lien ket', 'incident lien quan', 'incident khac',
            'follow', 'related incident', 'combined summary'))
        self.full_timeline = bool(re.search(
            r'all updates|full history|(?:full|complete) timeline|'
            r'toan bo[^.?!]*(?:timeline|cap nhat)|(?:timeline|cap nhat)[^.?!]*toan bo', text))
        self.providers = set()
        self.status_names = {}
        for schema in schemas:
            function = schema.get('function', {})
            properties = function.get('parameters', {}).get('properties', {})
            self.providers.update(properties.get('provider', {}).get('enum', []))
            match = re.fullmatch(r'get_(\w+)_status', function.get('name', ''))
            if match:
                self.status_names[function['name']] = match.group(1)
                self.providers.add(match.group(1))
        self.hosts = {urlsplit(url).hostname: provider for provider, url in HOSTS.items()
                      if provider in self.providers}
        self.mode = mode
        self.attempted = set()
        self.links = {}
        self.pages = {}
        self.read_pages = set()
        self.failures = {}
        self.incomplete = set()
        self.has_snapshot = False

    def record(self, name, arguments, observation):
        provider = self.status_names.get(name, arguments.get('provider', observation.get('provider', '')))
        incident = arguments.get('incident_id', '')
        provider = provider if isinstance(provider, str) else ''
        incident = incident if isinstance(incident, str) else ''
        key = (name, str(provider), str(incident))
        offset = arguments.get('offset', 0)
        if name == 'get_incident_details' and provider and incident:
            self.attempted.add((provider, incident))
            if type(offset) is int:
                self.read_pages.add((provider, incident, offset))
        error = observation.get('error') if observation.get('ok') is False else None
        if error:
            state = self.failures.setdefault(key, {'errors': {}, 'recovered': False})
            state['errors'][str(error)] = state['errors'].get(str(error), 0) + 1
            state['recovered'] = False
            return
        if key in self.failures:
            self.failures[key]['recovered'] = True
        self.has_snapshot |= bool(observation.get('captured_at')) and self.mode == 'snapshot'
        if name != 'get_incident_details' or not observation.get('ok'):
            return
        updates = observation.get('updates', [])
        if any(u.get('truncated') for u in updates):
            self.incomplete.add((provider, incident, 'body'))
        if observation.get('updates_total', len(updates)) > len(updates):
            self.incomplete.add((provider, incident, 'updates'))
        if 'next_offset' in observation:
            self.pages[(provider, incident)] = observation.get('next_offset')
        for update in updates:
            for url in re.findall(r'https?://[^\s<>"\)\]]+', update.get('body', '')):
                url = url.rstrip('.,;\'')
                parsed = urlsplit(url)
                match = re.fullmatch(r'/incidents/([A-Za-z0-9_-]+)', parsed.path.rstrip('/'))
                linked_provider = self.hosts.get(parsed.hostname)
                if linked_provider and match:
                    self.links[(linked_provider, match.group(1))] = url

    def pending(self):
        links = [{'provider': p, 'incident_id': i, 'url': url}
                 for (p, i), url in self.links.items()
                 if self.follow_links and (p, i) not in self.attempted]
        pages = [{'provider': p, 'incident_id': i, 'offset': offset}
                 for (p, i), offset in self.pages.items()
                 if self.full_timeline and offset is not None
                 and (p, i, offset) not in self.read_pages]
        return links, pages

    def notes(self):
        """Ghi chú do code tổng hợp từ trace, không thay đổi model message gốc."""
        lines = []
        for (name, provider, incident), state in self.failures.items():
            errors = ', '.join(f'{code}: {count} lần' for code, count in state['errors'].items())
            outcome = ('đã truy xuất thành công sau lỗi' if state['recovered']
                       else 'chưa có kết quả thành công cho truy vấn bị lỗi')
            target = f'{name} ({provider}' + (f', {incident}' if incident else '') + ')'
            lines.append(f'- {target}: {errors}; {outcome}. Lỗi truy xuất không xác định trạng thái dịch vụ.')
        if self.incomplete:
            lines.append('- Có body hoặc updates đã bị tool lược bớt; không đủ để khẳng định đã đọc toàn bộ nội dung/nguyên nhân.')
        if any(offset is not None for offset in self.pages.values()):
            lines.append('- Còn trang timeline chưa đọc; kết luận chỉ dựa trên các trang đã lấy.')
        if self.has_snapshot:
            lines.append('- Dữ liệu là snapshot theo captured_at, không xác nhận trạng thái thực tế tại thời điểm hiện tại.')
        return '\n'.join(lines)

    def unsupported_status_claims(self, answer):
        """Bắt khẳng định trạng thái trực tiếp khi query status chỉ có lỗi.

        Đây là guard cho câu dạng nhãn/trạng thái, không phải semantic fact checker.
        """
        unsupported = set()
        text = fold(answer)
        for (name, provider, _), state in self.failures.items():
            if state['recovered'] or not provider or not (name == 'get_status' or name in self.status_names):
                continue
            pattern = (r'\b' + re.escape(provider) + r'\b(?P<context>[^:\n|]{0,60})'
                       r'(?:[:|]|\bis\b)\s*[`*_\s]*(?:operational|outage|degraded|'
                       r'all systems operational|hoat dong binh thuong)\b')
            for match in re.finditer(pattern, text):
                if not re.search(r'\b(?:cannot|khong|chua|unknown|unable)\b', match.group('context')):
                    unsupported.add(provider)
        return sorted(unsupported)
