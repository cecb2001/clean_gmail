from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config


class GmailClient:
    """Wrapper for Gmail API operations."""

    def __init__(self, credentials):
        self.credentials = credentials
        self._service = None

    @property
    def service(self):
        """Lazy-load Gmail API service."""
        if self._service is None:
            self._service = build('gmail', 'v1', credentials=self.credentials)
        return self._service

    def get_profile(self):
        """Get user profile information."""
        try:
            profile = self.service.users().getProfile(userId='me').execute()
            return {
                'email': profile.get('emailAddress'),
                'messages_total': profile.get('messagesTotal', 0),
                'threads_total': profile.get('threadsTotal', 0),
                'history_id': profile.get('historyId'),
            }
        except HttpError as e:
            raise Exception(f"Failed to get profile: {e}")

    def get_labels(self):
        """Get all Gmail labels."""
        try:
            results = self.service.users().labels().list(userId='me').execute()
            return results.get('labels', [])
        except HttpError as e:
            raise Exception(f"Failed to get labels: {e}")

    def get_label_info(self, label_id):
        """Get detailed info for a specific label."""
        try:
            return self.service.users().labels().get(
                userId='me', id=label_id
            ).execute()
        except HttpError as e:
            raise Exception(f"Failed to get label {label_id}: {e}")

    def list_messages(self, query='', label_ids=None, max_results=None, page_token=None):
        """
        List messages matching the query.

        Args:
            query: Gmail search query (e.g., 'from:example.com')
            label_ids: List of label IDs to filter by
            max_results: Maximum number of messages to return
            page_token: Token for pagination

        Returns:
            dict with 'messages' list and 'nextPageToken'
        """
        try:
            params = {'userId': 'me'}

            if query:
                params['q'] = query
            if label_ids:
                params['labelIds'] = label_ids
            if max_results:
                params['maxResults'] = min(max_results, config.MAX_RESULTS_PER_PAGE)
            if page_token:
                params['pageToken'] = page_token

            results = self.service.users().messages().list(**params).execute()

            return {
                'messages': results.get('messages', []),
                'next_page_token': results.get('nextPageToken'),
                'result_size_estimate': results.get('resultSizeEstimate', 0),
            }
        except HttpError as e:
            raise Exception(f"Failed to list messages: {e}")

    def fetch_all_messages(self, query='', label_ids=None, max_total=None, progress_callback=None):
        """
        Fetch all messages matching criteria up to max_total.

        Args:
            query: Gmail search query
            label_ids: List of label IDs
            max_total: Maximum total messages to fetch
            progress_callback: Function to call with progress updates

        Yields:
            Message metadata dicts
        """
        # If max_total is None, fetch unlimited
        max_total = max_total if max_total is not None else float('inf')
        fetched = 0
        page_token = None
        page_num = 0

        print(f"[Fetch] Starting fetch with query='{query}', max_total={'unlimited' if max_total == float('inf') else max_total}", flush=True)

        while fetched < max_total:
            # For unlimited, just use max per page
            if max_total == float('inf'):
                page_size = config.MAX_RESULTS_PER_PAGE
            else:
                remaining = max_total - fetched
                page_size = min(remaining, config.MAX_RESULTS_PER_PAGE)

            result = self.list_messages(
                query=query,
                label_ids=label_ids,
                max_results=page_size,
                page_token=page_token
            )

            messages = result['messages']
            page_num += 1

            if not messages:
                print(f"[Fetch] Page {page_num}: No messages returned, stopping", flush=True)
                break

            print(f"[Fetch] Page {page_num}: Got {len(messages)} messages, estimate total: {result['result_size_estimate']}", flush=True)

            for msg in messages:
                yield msg
                fetched += 1
                if fetched >= max_total:
                    break

            if progress_callback:
                progress_callback(fetched, result['result_size_estimate'])

            page_token = result['next_page_token']
            if not page_token:
                print(f"[Fetch] No more pages after page {page_num}, total fetched: {fetched}", flush=True)
                break

        print(f"[Fetch] Complete. Total fetched: {fetched}", flush=True)

    def get_message(self, message_id, format='metadata', metadata_headers=None):
        """
        Get a single message.

        Args:
            message_id: The message ID
            format: 'minimal', 'metadata', 'raw', or 'full'
            metadata_headers: List of headers to include (for metadata format)

        Returns:
            Message dict
        """
        try:
            params = {
                'userId': 'me',
                'id': message_id,
                'format': format,
            }
            if metadata_headers:
                params['metadataHeaders'] = metadata_headers

            return self.service.users().messages().get(**params).execute()
        except HttpError as e:
            raise Exception(f"Failed to get message {message_id}: {e}")

    def get_messages_batch(self, message_ids, format='metadata', metadata_headers=None):
        """
        Get multiple messages in a batch request.

        Args:
            message_ids: List of message IDs
            format: Message format
            metadata_headers: Headers to include

        Returns:
            List of message dicts
        """
        import time

        if not metadata_headers:
            metadata_headers = ['From', 'To', 'Subject', 'Date']

        messages = []
        failed_ids = []

        # Create batch request
        batch = self.service.new_batch_http_request()

        def callback(request_id, response, exception):
            if exception:
                failed_ids.append(request_id)
                messages.append({'id': request_id, 'error': str(exception)})
            else:
                messages.append(response)

        for msg_id in message_ids:
            batch.add(
                self.service.users().messages().get(
                    userId='me',
                    id=msg_id,
                    format=format,
                    metadataHeaders=metadata_headers
                ),
                request_id=msg_id,
                callback=callback
            )

        try:
            batch.execute()
        except Exception as e:
            print(f"[Batch] Batch execute failed: {e}, falling back to individual fetches", flush=True)
            # Fallback: fetch individually
            messages = []
            for msg_id in message_ids:
                try:
                    msg = self.get_message(msg_id, format=format, metadata_headers=metadata_headers)
                    messages.append(msg)
                except Exception as individual_error:
                    messages.append({'id': msg_id, 'error': str(individual_error)})
                time.sleep(0.1)  # Rate limit protection

        # If too many failed in batch, retry failed ones individually
        if len(failed_ids) > len(message_ids) * 0.5:  # More than 50% failed
            print(f"[Batch] {len(failed_ids)} failed in batch, retrying individually", flush=True)
            # Remove failed entries and retry
            messages = [m for m in messages if m.get('id') not in failed_ids or 'error' not in m]
            for msg_id in failed_ids:
                try:
                    msg = self.get_message(msg_id, format=format, metadata_headers=metadata_headers)
                    messages.append(msg)
                except Exception as retry_error:
                    messages.append({'id': msg_id, 'error': str(retry_error)})
                time.sleep(0.1)

        return messages

    def trash_message(self, message_id):
        """Move a message to trash."""
        try:
            return self.service.users().messages().trash(
                userId='me', id=message_id
            ).execute()
        except HttpError as e:
            raise Exception(f"Failed to trash message {message_id}: {e}")

    def trash_messages_batch(self, message_ids, progress_callback=None):
        """
        Move multiple messages to trash in batches.

        Args:
            message_ids: List of message IDs to trash
            progress_callback: Function called with (processed, total)

        Returns:
            dict with 'success' count and 'errors' list
        """
        import time

        results = {'success': 0, 'errors': []}
        total = len(message_ids)

        for i in range(0, total, config.BATCH_DELETE_SIZE):
            batch_ids = message_ids[i:i + config.BATCH_DELETE_SIZE]

            batch = self.service.new_batch_http_request()

            def make_callback(msg_id):
                def callback(request_id, response, exception):
                    if exception:
                        results['errors'].append({
                            'id': msg_id,
                            'error': str(exception)
                        })
                    else:
                        results['success'] += 1
                return callback

            for msg_id in batch_ids:
                batch.add(
                    self.service.users().messages().trash(
                        userId='me', id=msg_id
                    ),
                    callback=make_callback(msg_id)
                )

            batch.execute()

            if progress_callback:
                progress_callback(min(i + config.BATCH_DELETE_SIZE, total), total)

            # Add delay between batches to avoid rate limiting
            if i + config.BATCH_DELETE_SIZE < total:
                time.sleep(0.5)

        return results

    def delete_message_permanently(self, message_id):
        """Permanently delete a message (cannot be undone)."""
        try:
            self.service.users().messages().delete(
                userId='me', id=message_id
            ).execute()
            return True
        except HttpError as e:
            raise Exception(f"Failed to delete message {message_id}: {e}")

    def delete_messages_permanently_batch(self, message_ids, progress_callback=None):
        """
        Permanently delete multiple messages in batches.

        Args:
            message_ids: List of message IDs to delete
            progress_callback: Function called with (processed, total)

        Returns:
            dict with 'success' count and 'errors' list
        """
        import time

        results = {'success': 0, 'errors': []}
        total = len(message_ids)

        for i in range(0, total, config.BATCH_DELETE_SIZE):
            batch_ids = message_ids[i:i + config.BATCH_DELETE_SIZE]

            batch = self.service.new_batch_http_request()

            def make_callback(msg_id):
                def callback(request_id, response, exception):
                    if exception:
                        results['errors'].append({
                            'id': msg_id,
                            'error': str(exception)
                        })
                    else:
                        results['success'] += 1
                return callback

            for msg_id in batch_ids:
                batch.add(
                    self.service.users().messages().delete(
                        userId='me', id=msg_id
                    ),
                    callback=make_callback(msg_id)
                )

            batch.execute()

            if progress_callback:
                progress_callback(min(i + config.BATCH_DELETE_SIZE, total), total)

            # Add delay between batches to avoid rate limiting
            if i + config.BATCH_DELETE_SIZE < total:
                time.sleep(0.5)

        return results

    def list_trash_messages(self, max_results=None, page_token=None):
        """List messages in trash."""
        return self.list_messages(
            label_ids=['TRASH'],
            max_results=max_results,
            page_token=page_token
        )

    def get_message_full(self, message_id):
        """
        Get full message content including body.

        Returns:
            dict with headers and body content
        """
        import base64
        from email.utils import parseaddr, parsedate_to_datetime

        try:
            msg = self.service.users().messages().get(
                userId='me',
                id=message_id,
                format='full'
            ).execute()

            # Extract headers
            headers = {}
            for header in msg.get('payload', {}).get('headers', []):
                headers[header['name'].lower()] = header['value']

            # Extract body
            body_html = ''
            body_text = ''

            def extract_body(payload):
                nonlocal body_html, body_text

                mime_type = payload.get('mimeType', '')
                body_data = payload.get('body', {}).get('data', '')

                if body_data:
                    decoded = base64.urlsafe_b64decode(body_data).decode('utf-8', errors='replace')
                    if mime_type == 'text/html':
                        body_html = decoded
                    elif mime_type == 'text/plain':
                        body_text = decoded

                # Recurse into parts
                for part in payload.get('parts', []):
                    extract_body(part)

            extract_body(msg.get('payload', {}))

            # Parse sender
            from_header = headers.get('from', '')
            sender_name, sender_email = parseaddr(from_header)

            # Parse date
            date_str = headers.get('date', '')
            try:
                date = parsedate_to_datetime(date_str)
                date_formatted = date.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                date_formatted = date_str

            # Extract unsubscribe links
            unsubscribe_links = self._extract_unsubscribe_links(headers, body_html, body_text)

            return {
                'id': message_id,
                'thread_id': msg.get('threadId'),
                'labels': msg.get('labelIds', []),
                'from': sender_email,
                'from_name': sender_name,
                'to': headers.get('to', ''),
                'subject': headers.get('subject', '(no subject)'),
                'date': date_formatted,
                'snippet': msg.get('snippet', ''),
                'size_bytes': msg.get('sizeEstimate', 0),
                'body_html': body_html,
                'body_text': body_text,
                'unsubscribe_links': unsubscribe_links,
            }

        except HttpError as e:
            raise Exception(f"Failed to get message {message_id}: {e}")

    def _extract_unsubscribe_links(self, headers, body_html, body_text):
        """
        Extract unsubscribe links from email headers and body.

        Returns:
            dict with 'header_links' (from List-Unsubscribe) and 'body_links' (from HTML body)
        """
        import re
        from html.parser import HTMLParser

        result = {
            'header_links': [],
            'body_links': [],
            'mailto': None,
            'list_unsubscribe_post': headers.get('list-unsubscribe-post', ''),
        }

        # Extract from List-Unsubscribe header
        list_unsubscribe = headers.get('list-unsubscribe', '')
        if list_unsubscribe:
            # Parse URLs and mailto links from header
            # Format: <mailto:unsub@example.com>, <https://example.com/unsub>
            links = re.findall(r'<([^>]+)>', list_unsubscribe)
            for link in links:
                if link.startswith('mailto:'):
                    result['mailto'] = link
                elif link.startswith('http://') or link.startswith('https://'):
                    result['header_links'].append(link)

        # Extract from HTML body
        if body_html:
            class UnsubscribeLinkParser(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.links = []
                    self.current_href = None
                    self.in_link = False
                    self.link_text = ''

                def handle_starttag(self, tag, attrs):
                    if tag == 'a':
                        self.in_link = True
                        self.link_text = ''
                        for attr, value in attrs:
                            if attr == 'href':
                                self.current_href = value

                def handle_endtag(self, tag):
                    if tag == 'a' and self.in_link:
                        self.in_link = False
                        if self.current_href:
                            # Check if link text contains unsubscribe-related words
                            text_lower = self.link_text.lower()
                            if any(word in text_lower for word in ['unsubscribe', 'opt out', 'opt-out', 'remove', 'manage preferences', 'email preferences', 'stop receiving']):
                                if self.current_href.startswith('http://') or self.current_href.startswith('https://'):
                                    self.links.append({
                                        'url': self.current_href,
                                        'text': self.link_text.strip()
                                    })
                        self.current_href = None

                def handle_data(self, data):
                    if self.in_link:
                        self.link_text += data

            try:
                parser = UnsubscribeLinkParser()
                parser.feed(body_html)
                result['body_links'] = parser.links[:5]  # Limit to first 5 matches
            except Exception:
                pass  # Ignore parsing errors

        return result

    def get_unsubscribe_link_for_sender(self, sender_email, max_emails=5):
        """
        Find unsubscribe link by checking recent emails from a sender.

        Args:
            sender_email: Email address of the sender
            max_emails: Maximum emails to check

        Returns:
            dict with unsubscribe info or None
        """
        try:
            # Search for recent emails from this sender
            query = f'from:{sender_email}'
            result = self.list_messages(query=query, max_results=max_emails)
            messages = result.get('messages', [])

            if not messages:
                return None

            # Check each message for unsubscribe links
            for msg in messages[:max_emails]:
                try:
                    full_msg = self.get_message_full(msg['id'])
                    unsub = full_msg.get('unsubscribe_links', {})

                    # Prefer header links, then body links
                    if unsub.get('header_links'):
                        return {
                            'type': 'header',
                            'url': unsub['header_links'][0],
                            'mailto': unsub.get('mailto'),
                            'source_email_id': msg['id'],
                            'sender': sender_email
                        }
                    elif unsub.get('body_links'):
                        return {
                            'type': 'body',
                            'url': unsub['body_links'][0]['url'],
                            'text': unsub['body_links'][0]['text'],
                            'source_email_id': msg['id'],
                            'sender': sender_email
                        }
                except Exception as e:
                    print(f"[Unsubscribe] Error checking email {msg['id']}: {e}", flush=True)
                    continue

            return None

        except Exception as e:
            print(f"[Unsubscribe] Error finding unsubscribe for {sender_email}: {e}", flush=True)
            return None

    def send_unsubscribe_email(self, to_address, subject='unsubscribe', body='unsubscribe'):
        """Send an unsubscribe email via Gmail API for mailto: links."""
        import base64
        from email.mime.text import MIMEText

        message = MIMEText(body)
        message['to'] = to_address
        message['subject'] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

        return self.service.users().messages().send(
            userId='me', body={'raw': raw}
        ).execute()
