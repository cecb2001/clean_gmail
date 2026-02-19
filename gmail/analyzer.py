import re
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime

import config


class EmailAnalyzer:
    """Analyzes emails and detects patterns for cleanup suggestions."""

    def __init__(self, gmail_client):
        self.client = gmail_client
        self._emails = []
        self._analysis_cache = None

    def fetch_and_analyze(self, query='', label_ids=None, max_emails=None, progress_callback=None):
        """
        Fetch emails and perform full analysis.

        Args:
            query: Gmail search query to filter emails
            label_ids: Filter by specific labels
            max_emails: Maximum emails to analyze
            progress_callback: Function called with (fetched, total, stage)

        Returns:
            Analysis results dict
        """
        max_emails = max_emails or config.MAX_EMAILS_TO_ANALYZE
        self._emails = []
        self._analysis_cache = None

        # Stage 1: Fetch message IDs
        if progress_callback:
            progress_callback(0, 0, 'fetching_ids')

        message_ids = []
        for msg in self.client.fetch_all_messages(
            query=query,
            label_ids=label_ids,
            max_total=max_emails
        ):
            message_ids.append(msg['id'])

        if not message_ids:
            return self._empty_analysis()

        # Stage 2: Fetch message metadata in batches
        if progress_callback:
            progress_callback(0, len(message_ids), 'fetching_metadata')

        metadata_headers = ['From', 'To', 'Subject', 'Date', 'List-Unsubscribe']
        batch_size = 100

        for i in range(0, len(message_ids), batch_size):
            batch_ids = message_ids[i:i + batch_size]
            messages = self.client.get_messages_batch(
                batch_ids,
                format='metadata',
                metadata_headers=metadata_headers
            )

            for msg in messages:
                if 'error' not in msg:
                    self._emails.append(self._parse_message(msg))

            if progress_callback:
                progress_callback(
                    min(i + batch_size, len(message_ids)),
                    len(message_ids),
                    'fetching_metadata'
                )

        # Stage 3: Analyze patterns
        if progress_callback:
            progress_callback(0, 0, 'analyzing')

        return self.analyze()

    def _parse_message(self, msg):
        """Parse message metadata into a normalized format."""
        headers = {}
        for header in msg.get('payload', {}).get('headers', []):
            headers[header['name'].lower()] = header['value']

        # Parse sender
        from_header = headers.get('from', '')
        sender_name, sender_email = parseaddr(from_header)
        sender_domain = sender_email.split('@')[-1] if '@' in sender_email else ''

        # Parse date
        date_str = headers.get('date', '')
        try:
            date = parsedate_to_datetime(date_str)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
        except Exception:
            date = datetime.now(timezone.utc)

        # Calculate age in days
        now = datetime.now(timezone.utc)
        age_days = (now - date).days

        return {
            'id': msg['id'],
            'thread_id': msg.get('threadId'),
            'labels': msg.get('labelIds', []),
            'snippet': msg.get('snippet', ''),
            'size_bytes': msg.get('sizeEstimate', 0),
            'sender_email': sender_email.lower(),
            'sender_name': sender_name,
            'sender_domain': sender_domain.lower(),
            'subject': headers.get('subject', ''),
            'date': date,
            'age_days': age_days,
            'has_unsubscribe': 'list-unsubscribe' in headers,
            'is_unread': 'UNREAD' in msg.get('labelIds', []),
        }

    def analyze(self):
        """Perform pattern analysis on fetched emails."""
        if self._analysis_cache:
            return self._analysis_cache

        if not self._emails:
            return self._empty_analysis()

        analysis = {
            'total_emails': len(self._emails),
            'total_size_bytes': sum(e['size_bytes'] for e in self._emails),
            'patterns': {
                'by_sender_domain': self._group_by_sender_domain(),
                'by_sender_email': self._group_by_sender_email(),
                'by_category': self._group_by_category(),
                'by_age': self._group_by_age(),
                'by_size': self._group_by_size(),
                'newsletters': self._find_newsletters(),
            },
            'summary': self._generate_summary(),
        }

        self._analysis_cache = analysis
        return analysis

    def _group_by_sender_domain(self):
        """Group emails by sender domain."""
        groups = defaultdict(lambda: {'count': 0, 'size_bytes': 0, 'emails': [], 'unread': 0})

        for email in self._emails:
            domain = email['sender_domain']
            if domain:
                groups[domain]['count'] += 1
                groups[domain]['size_bytes'] += email['size_bytes']
                groups[domain]['emails'].append(email['id'])
                if email['is_unread']:
                    groups[domain]['unread'] += 1

        # Convert to sorted list
        result = []
        for domain, data in groups.items():
            if data['count'] >= config.MIN_PATTERN_COUNT:
                result.append({
                    'type': 'sender_domain',
                    'key': domain,
                    'display': domain,
                    'count': data['count'],
                    'size_bytes': data['size_bytes'],
                    'unread': data['unread'],
                    'email_ids': data['emails'],
                })

        return sorted(result, key=lambda x: x['size_bytes'], reverse=True)

    def _group_by_sender_email(self):
        """Group emails by exact sender email."""
        groups = defaultdict(lambda: {'count': 0, 'size_bytes': 0, 'emails': [], 'unread': 0, 'name': ''})

        for email in self._emails:
            sender = email['sender_email']
            if sender:
                groups[sender]['count'] += 1
                groups[sender]['size_bytes'] += email['size_bytes']
                groups[sender]['emails'].append(email['id'])
                if email['is_unread']:
                    groups[sender]['unread'] += 1
                if not groups[sender]['name'] and email['sender_name']:
                    groups[sender]['name'] = email['sender_name']

        result = []
        for sender, data in groups.items():
            if data['count'] >= config.MIN_PATTERN_COUNT:
                result.append({
                    'type': 'sender_email',
                    'key': sender,
                    'display': f"{data['name']} <{sender}>" if data['name'] else sender,
                    'count': data['count'],
                    'size_bytes': data['size_bytes'],
                    'unread': data['unread'],
                    'email_ids': data['emails'],
                })

        return sorted(result, key=lambda x: x['size_bytes'], reverse=True)

    def _group_by_category(self):
        """Group emails by Gmail categories."""
        groups = defaultdict(lambda: {'count': 0, 'size_bytes': 0, 'emails': [], 'unread': 0})

        category_display = {
            'CATEGORY_PROMOTIONS': 'Promotions',
            'CATEGORY_SOCIAL': 'Social',
            'CATEGORY_UPDATES': 'Updates',
            'CATEGORY_FORUMS': 'Forums',
            'CATEGORY_PERSONAL': 'Primary',
        }

        for email in self._emails:
            for label in email['labels']:
                if label.startswith('CATEGORY_'):
                    groups[label]['count'] += 1
                    groups[label]['size_bytes'] += email['size_bytes']
                    groups[label]['emails'].append(email['id'])
                    if email['is_unread']:
                        groups[label]['unread'] += 1

        result = []
        for category, data in groups.items():
            result.append({
                'type': 'category',
                'key': category,
                'display': category_display.get(category, category),
                'count': data['count'],
                'size_bytes': data['size_bytes'],
                'unread': data['unread'],
                'email_ids': data['emails'],
            })

        return sorted(result, key=lambda x: x['size_bytes'], reverse=True)

    def _group_by_age(self):
        """Group emails by age buckets."""
        age_groups = config.AGE_GROUPS_DAYS
        groups = {days: {'count': 0, 'size_bytes': 0, 'emails': [], 'unread': 0}
                  for days in age_groups}

        for email in self._emails:
            for days in age_groups:
                if email['age_days'] >= days:
                    groups[days]['count'] += 1
                    groups[days]['size_bytes'] += email['size_bytes']
                    groups[days]['emails'].append(email['id'])
                    if email['is_unread']:
                        groups[days]['unread'] += 1
                    break  # Only count in the oldest applicable bucket

        result = []
        labels = {
            30: 'Older than 1 month',
            90: 'Older than 3 months',
            180: 'Older than 6 months',
            365: 'Older than 1 year',
            730: 'Older than 2 years',
        }

        for days in age_groups:
            data = groups[days]
            if data['count'] >= config.MIN_PATTERN_COUNT:
                result.append({
                    'type': 'age',
                    'key': f'older_than_{days}_days',
                    'display': labels.get(days, f'Older than {days} days'),
                    'count': data['count'],
                    'size_bytes': data['size_bytes'],
                    'unread': data['unread'],
                    'email_ids': data['emails'],
                    'age_days': days,
                })

        return result  # Keep in age order, oldest first

    def _group_by_size(self):
        """Find large emails."""
        threshold = config.LARGE_EMAIL_SIZE_KB * 1024  # Convert to bytes
        large_emails = []

        for email in self._emails:
            if email['size_bytes'] >= threshold:
                large_emails.append(email)

        if len(large_emails) < config.MIN_PATTERN_COUNT:
            return []

        # Sort by size, largest first
        large_emails.sort(key=lambda x: x['size_bytes'], reverse=True)

        return [{
            'type': 'size',
            'key': 'large_emails',
            'display': f'Large emails (>{config.LARGE_EMAIL_SIZE_KB}KB)',
            'count': len(large_emails),
            'size_bytes': sum(e['size_bytes'] for e in large_emails),
            'unread': sum(1 for e in large_emails if e['is_unread']),
            'email_ids': [e['id'] for e in large_emails],
        }]

    def _find_newsletters(self):
        """Find newsletter/mailing list emails."""
        newsletters = [e for e in self._emails if e['has_unsubscribe']]

        if len(newsletters) < config.MIN_PATTERN_COUNT:
            return []

        unread_newsletters = [e for e in newsletters if e['is_unread']]

        result = [{
            'type': 'newsletter',
            'key': 'all_newsletters',
            'display': 'Newsletters & Mailing Lists',
            'count': len(newsletters),
            'size_bytes': sum(e['size_bytes'] for e in newsletters),
            'unread': len(unread_newsletters),
            'email_ids': [e['id'] for e in newsletters],
        }]

        if len(unread_newsletters) >= config.MIN_PATTERN_COUNT:
            result.append({
                'type': 'newsletter',
                'key': 'unread_newsletters',
                'display': 'Unread Newsletters',
                'count': len(unread_newsletters),
                'size_bytes': sum(e['size_bytes'] for e in unread_newsletters),
                'unread': len(unread_newsletters),
                'email_ids': [e['id'] for e in unread_newsletters],
            })

        return result

    def _generate_summary(self):
        """Generate a summary of the analysis."""
        unread = sum(1 for e in self._emails if e['is_unread'])
        newsletters = sum(1 for e in self._emails if e['has_unsubscribe'])

        # Find the most common senders
        sender_counts = defaultdict(int)
        for e in self._emails:
            sender_counts[e['sender_domain']] += 1
        top_senders = sorted(sender_counts.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            'total_emails': len(self._emails),
            'total_size_mb': round(sum(e['size_bytes'] for e in self._emails) / (1024 * 1024), 2),
            'unread_count': unread,
            'newsletter_count': newsletters,
            'top_senders': [{'domain': d, 'count': c} for d, c in top_senders],
        }

    def _empty_analysis(self):
        """Return empty analysis structure."""
        return {
            'total_emails': 0,
            'total_size_bytes': 0,
            'patterns': {
                'by_sender_domain': [],
                'by_sender_email': [],
                'by_category': [],
                'by_age': [],
                'by_size': [],
                'newsletters': [],
            },
            'summary': {
                'total_emails': 0,
                'total_size_mb': 0,
                'unread_count': 0,
                'newsletter_count': 0,
                'top_senders': [],
            },
        }

    def get_email_samples(self, email_ids, limit=10):
        """Get sample email details for preview."""
        sample_ids = email_ids[:limit]
        samples = []

        messages = self.client.get_messages_batch(
            sample_ids,
            format='metadata',
            metadata_headers=['From', 'Subject', 'Date']
        )

        for msg in messages:
            if 'error' not in msg:
                parsed = self._parse_message(msg)
                samples.append({
                    'id': parsed['id'],
                    'from': parsed['sender_email'],
                    'from_name': parsed['sender_name'],
                    'subject': parsed['subject'],
                    'date': parsed['date'].strftime('%Y-%m-%d %H:%M'),
                    'snippet': msg.get('snippet', '')[:100],
                    'size_kb': round(parsed['size_bytes'] / 1024, 1),
                })

        return samples

    def get_emails_for_pattern(self, pattern_type, pattern_key):
        """Get all email IDs matching a specific pattern."""
        if not self._analysis_cache:
            return []

        patterns = self._analysis_cache['patterns']

        # Find the matching pattern
        for category in patterns.values():
            for pattern in category:
                if pattern['type'] == pattern_type and pattern['key'] == pattern_key:
                    return pattern['email_ids']

        return []
