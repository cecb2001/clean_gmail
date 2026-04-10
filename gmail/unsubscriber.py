import json
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

import requests

import config


class UnsubscribeManager:
    """Manages programmatic email unsubscription."""

    USER_AGENT = (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    )

    def __init__(self, gmail_client, tracking_file=None):
        self.client = gmail_client
        self.tracking_file = tracking_file or config.UNSUBSCRIBE_TRACKING_FILE

    def load_tracking(self):
        try:
            if Path(self.tracking_file).exists():
                with open(self.tracking_file, 'r') as f:
                    return json.load(f)
        except Exception as e:
            print(f"[Unsub] Failed to load tracking: {e}", flush=True)
        return {'unsubscribed': []}

    def save_tracking(self, data):
        with open(self.tracking_file, 'w') as f:
            json.dump(data, f, indent=2)

    def is_already_unsubscribed(self, sender_email):
        tracking = self.load_tracking()
        return any(
            entry['sender_email'] == sender_email and entry['status'] in ('success', 'unknown')
            for entry in tracking.get('unsubscribed', [])
        )

    def get_unsubscribed_senders(self):
        return self.load_tracking().get('unsubscribed', [])

    def execute_unsubscribe(self, sender_email, unsub_info, display_name=''):
        """
        Attempt to unsubscribe from a sender.

        Args:
            sender_email: The sender's email address
            unsub_info: Dict from get_unsubscribe_link_for_sender with keys:
                        type, url, mailto, list_unsubscribe_post, etc.
            display_name: Sender display name for tracking

        Returns:
            dict with status, method, details
        """
        result = {
            'sender_email': sender_email,
            'sender_domain': sender_email.split('@')[-1] if '@' in sender_email else '',
            'display_name': display_name,
            'method': None,
            'url': None,
            'status': 'failed',
            'attempted_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'http_status_code': None,
            'error': None,
        }

        try:
            # Priority 1: RFC 8058 one-click POST
            if unsub_info.get('list_unsubscribe_post') and unsub_info.get('header_links'):
                url = unsub_info['header_links'][0]
                result['method'] = 'http_post'
                result['url'] = url
                http_result = self._unsubscribe_via_http_post(url)
                result.update(http_result)

            # Priority 2: Header HTTP link via GET
            elif unsub_info.get('header_links'):
                url = unsub_info['header_links'][0]
                result['method'] = 'http_get'
                result['url'] = url
                http_result = self._unsubscribe_via_http_get(url)
                result.update(http_result)

            # Priority 3: Body HTTP link via GET
            elif unsub_info.get('body_links'):
                link = unsub_info['body_links'][0]
                url = link['url'] if isinstance(link, dict) else link
                result['method'] = 'http_get'
                result['url'] = url
                http_result = self._unsubscribe_via_http_get(url)
                result.update(http_result)

            # Priority 4: mailto link
            elif unsub_info.get('mailto'):
                result['method'] = 'mailto'
                result['url'] = unsub_info['mailto']
                mailto_result = self._unsubscribe_via_mailto(unsub_info['mailto'])
                result.update(mailto_result)

            else:
                result['error'] = 'No unsubscribe method available'

        except Exception as e:
            result['error'] = str(e)
            result['status'] = 'failed'

        # Save to tracking
        self._record_attempt(result)
        return result

    def _unsubscribe_via_http_post(self, url):
        """RFC 8058 one-click unsubscribe via POST."""
        try:
            resp = requests.post(
                url,
                data='List-Unsubscribe=One-Click',
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'User-Agent': self.USER_AGENT,
                },
                timeout=config.UNSUBSCRIBE_HTTP_TIMEOUT,
                allow_redirects=True,
            )
            return {
                'http_status_code': resp.status_code,
                'status': 'unknown' if 200 <= resp.status_code < 400 else 'failed',
                'error': None if 200 <= resp.status_code < 400 else f'HTTP {resp.status_code}',
            }
        except requests.RequestException as e:
            return {'status': 'failed', 'error': str(e)}

    def _unsubscribe_via_http_get(self, url):
        """Unsubscribe via HTTP GET request."""
        try:
            resp = requests.get(
                url,
                headers={'User-Agent': self.USER_AGENT},
                timeout=config.UNSUBSCRIBE_HTTP_TIMEOUT,
                allow_redirects=True,
            )
            return {
                'http_status_code': resp.status_code,
                'status': 'unknown' if 200 <= resp.status_code < 400 else 'failed',
                'error': None if 200 <= resp.status_code < 400 else f'HTTP {resp.status_code}',
            }
        except requests.RequestException as e:
            return {'status': 'failed', 'error': str(e)}

    def _unsubscribe_via_mailto(self, mailto_link):
        """Unsubscribe by sending an email via Gmail API."""
        try:
            # Parse mailto: URL
            mailto = mailto_link
            if mailto.startswith('mailto:'):
                mailto = mailto[7:]

            # Split address and params
            if '?' in mailto:
                address, params_str = mailto.split('?', 1)
                params = parse_qs(params_str)
            else:
                address = mailto
                params = {}

            address = unquote(address)
            subject = params.get('subject', ['unsubscribe'])[0]
            body = params.get('body', ['unsubscribe'])[0]

            self.client.send_unsubscribe_email(address, subject, body)
            return {'status': 'unknown', 'error': None}

        except Exception as e:
            return {'status': 'failed', 'error': str(e)}

    def _record_attempt(self, result):
        tracking = self.load_tracking()
        # Remove any previous attempt for same sender
        tracking['unsubscribed'] = [
            entry for entry in tracking.get('unsubscribed', [])
            if entry['sender_email'] != result['sender_email']
        ]
        tracking['unsubscribed'].append(result)
        self.save_tracking(tracking)

    def get_suggestions(self, analysis_data, email_index):
        """
        Score senders for unsubscribe suggestions.

        Args:
            analysis_data: Cached analysis result with patterns
            email_index: Per-email metadata index

        Returns:
            Sorted list of suggestion dicts
        """
        tracking = self.load_tracking()
        unsubscribed_senders = {
            entry['sender_email']
            for entry in tracking.get('unsubscribed', [])
            if entry['status'] in ('success', 'unknown')
        }

        # Build sender -> stats from the by_sender_email patterns
        patterns = analysis_data.get('patterns', {})
        sender_patterns = patterns.get('by_sender_email', [])

        # Build set of newsletter email IDs for cross-reference
        newsletter_ids = set()
        for np in patterns.get('newsletters', []):
            newsletter_ids.update(np.get('email_ids', []))

        # Build sender -> has_unsubscribe from email index
        sender_has_unsub = {}
        for eid, data in email_index.items():
            if data.get('hu'):
                se = data.get('se', '')
                if se:
                    sender_has_unsub[se] = True

        suggestions = []
        for pattern in sender_patterns:
            sender = pattern['key']

            if sender in unsubscribed_senders:
                continue

            if not sender_has_unsub.get(sender):
                continue

            count = pattern.get('count', 0)
            unread = pattern.get('unread', 0)

            if count < config.UNSUBSCRIBE_SUGGESTION_MIN_EMAILS:
                continue

            unread_ratio = unread / count if count > 0 else 0

            # Score calculation
            score = 0
            reasons = []

            if unread_ratio >= 0.9:
                score += 30
                reasons.append('Almost all unread')
            elif unread_ratio >= 0.7:
                score += 20
                reasons.append('Mostly unread')

            if count >= 50:
                score += 20
                reasons.append('High volume')
            elif count >= 20:
                score += 10
                reasons.append('Frequent sender')

            # Check if in promotions/social
            email_ids = pattern.get('email_ids', [])
            promo_social = False
            for eid in email_ids[:20]:
                edata = email_index.get(eid, {})
                labels = edata.get('lb', [])
                if 'CATEGORY_PROMOTIONS' in labels or 'CATEGORY_SOCIAL' in labels:
                    promo_social = True
                    break
            if promo_social:
                score += 10
                reasons.append('Promotions/Social')

            if score == 0:
                continue

            suggestions.append({
                'sender_email': sender,
                'sender_domain': sender.split('@')[-1] if '@' in sender else '',
                'display_name': pattern.get('display', sender),
                'email_count': count,
                'unread_count': unread,
                'unread_ratio': round(unread_ratio, 2),
                'score': score,
                'reasons': reasons,
                'size_bytes': pattern.get('size_bytes', 0),
            })

        suggestions.sort(key=lambda x: x['score'], reverse=True)
        return suggestions

    def batch_unsubscribe(self, senders_info, progress_callback=None):
        """
        Batch unsubscribe from multiple senders.

        Args:
            senders_info: List of dicts with sender_email, unsub_info, display_name
            progress_callback: Function called with (current, total, result)

        Returns:
            dict with success/failed counts and details
        """
        results = {'success': 0, 'failed': 0, 'details': []}
        total = len(senders_info)

        for i, info in enumerate(senders_info):
            result = self.execute_unsubscribe(
                info['sender_email'],
                info['unsub_info'],
                info.get('display_name', ''),
            )
            if result['status'] in ('success', 'unknown'):
                results['success'] += 1
            else:
                results['failed'] += 1
            results['details'].append(result)

            if progress_callback:
                progress_callback(i + 1, total, result)

            if i + 1 < total:
                time.sleep(config.UNSUBSCRIBE_BATCH_DELAY)

        return results
