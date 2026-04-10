import os

# Allow OAuth over HTTP for local development
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

from flask import Flask, render_template, redirect, url_for, request, session, jsonify, Response
import json
import time

import config
from gmail import GmailAuth, GmailClient, EmailAnalyzer, RuleEngine, UnsubscribeManager

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# Global state for session data
_auth = GmailAuth()

# Server-side storage for analysis data (keyed by user email)
_analysis_cache = {}
_analysis_job = {
    'running': False,
    'progress': None,
    'result': None,
    'error': None
}
_delete_job = {
    'running': False,
    'progress': None,
    'result': None,
    'error': None,
    'email_ids': [],
    'permanent': False
}
_rule_job = {
    'running': False,
    'progress': None,
    'result': None,
    'error': None,
    'rule_id': None,
}
_unsubscribe_job = {
    'running': False,
    'progress': None,
    'result': None,
    'error': None,
}

# File to persist analysis data
ANALYSIS_CACHE_FILE = 'analysis_cache.json'
EMAIL_INDEX_FILE = str(config.EMAIL_INDEX_FILE)
USER_ACTIONS_FILE = str(config.USER_ACTIONS_FILE)


def load_user_actions():
    """Load user action history from file."""
    try:
        if os.path.exists(USER_ACTIONS_FILE):
            with open(USER_ACTIONS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"[Actions] Failed to load: {e}", flush=True)
    return {'deleted': {}, 'dismissed': {}, 'kept': {}}


def save_user_actions(actions):
    """Save user action history to file."""
    try:
        with open(USER_ACTIONS_FILE, 'w') as f:
            json.dump(actions, f, indent=2)
    except Exception as e:
        print(f"[Actions] Failed to save: {e}", flush=True)


def _record_deleted_senders(deleted_ids):
    """Record sender info for deleted emails in user_actions.json."""
    index = load_email_index()
    if not index:
        return
    actions = load_user_actions()
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    for eid in deleted_ids:
        entry = index.get(eid)
        if not entry:
            continue
        for prefix, key in [('sender_domain', entry.get('sd')),
                            ('sender_email', entry.get('se'))]:
            if not key:
                continue
            action_key = f'{prefix}:{key}'
            existing = actions['deleted'].get(action_key, {'email_count': 0})
            existing['email_count'] = existing.get('email_count', 0) + 1
            existing['last_action'] = now
            actions['deleted'][action_key] = existing
    save_user_actions(actions)


def save_email_index(index):
    """Save email index to file."""
    try:
        with open(EMAIL_INDEX_FILE, 'w') as f:
            json.dump(index, f)
        print(f"[Index] Saved email index ({len(index):,} emails) to {EMAIL_INDEX_FILE}", flush=True)
    except Exception as e:
        print(f"[Index] Failed to save: {e}", flush=True)

def load_email_index():
    """Load email index from file."""
    try:
        if os.path.exists(EMAIL_INDEX_FILE):
            with open(EMAIL_INDEX_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"[Index] Failed to load: {e}", flush=True)
    return None

def _update_email_index(deleted_ids):
    """Remove deleted IDs from the email index file."""
    index = load_email_index()
    if not index:
        return
    deleted_set = set(deleted_ids) if not isinstance(deleted_ids, set) else deleted_ids
    for eid in deleted_set:
        index.pop(eid, None)
    save_email_index(index)

def save_analysis_to_file(data):
    """Save analysis data to file for persistence."""
    try:
        with open(ANALYSIS_CACHE_FILE, 'w') as f:
            json.dump(data, f)
        print(f"[Cache] Saved analysis to {ANALYSIS_CACHE_FILE}", flush=True)
    except Exception as e:
        print(f"[Cache] Failed to save analysis: {e}", flush=True)

def load_analysis_from_file():
    """Load analysis data from file."""
    try:
        if os.path.exists(ANALYSIS_CACHE_FILE):
            with open(ANALYSIS_CACHE_FILE, 'r') as f:
                data = json.load(f)
            print(f"[Cache] Loaded analysis from {ANALYSIS_CACHE_FILE}", flush=True)
            return data
    except Exception as e:
        print(f"[Cache] Failed to load analysis: {e}", flush=True)
    return None


def _update_cached_analysis(deleted_ids, save_to_file=True):
    """Remove deleted email IDs from cached analysis data and persist to file."""
    analysis = _analysis_cache.get('last_analysis')
    if not analysis:
        # Try to load from file if not in memory
        analysis = load_analysis_from_file()
        if analysis:
            _analysis_cache['last_analysis'] = analysis
        else:
            return

    deleted_ids_set = set(deleted_ids) if not isinstance(deleted_ids, set) else deleted_ids
    patterns = analysis.get('patterns', {})

    # Update each category of patterns
    for category_name, category_patterns in patterns.items():
        for pattern in category_patterns:
            original_ids = pattern.get('email_ids', [])
            original_count = len(original_ids)

            # Remove deleted IDs
            remaining_ids = [eid for eid in original_ids if eid not in deleted_ids_set]

            # Calculate how many were deleted in this update
            newly_deleted = original_count - len(remaining_ids)

            if newly_deleted > 0:
                pattern['email_ids'] = remaining_ids
                pattern['count'] = len(remaining_ids)
                pattern['deleted_count'] = pattern.get('deleted_count', 0) + newly_deleted

                # Track original count for UI display
                if 'original_count' not in pattern:
                    pattern['original_count'] = original_count + pattern.get('deleted_count', 0) - newly_deleted

                # Estimate size reduction (proportional)
                if original_count > 0:
                    ratio = len(remaining_ids) / original_count
                    original_size = pattern.get('size_bytes', 0)
                    pattern['size_bytes'] = int(original_size * ratio)
                    # Track deleted size
                    pattern['deleted_size_bytes'] = pattern.get('deleted_size_bytes', 0) + int(original_size * (1 - ratio))

    # Update summary totals
    if 'summary' in analysis:
        total_deleted = len(deleted_ids_set)
        analysis['summary']['total_emails'] = max(0, analysis['summary'].get('total_emails', 0) - total_deleted)
        analysis['summary']['deleted_count'] = analysis['summary'].get('deleted_count', 0) + total_deleted

    # Persist to file
    if save_to_file:
        save_analysis_to_file(analysis)
        _update_email_index(deleted_ids_set)
        print(f"[Cache] Updated analysis cache after deleting {len(deleted_ids_set)} emails", flush=True)


def get_gmail_client():
    """Get authenticated Gmail client."""
    creds = _auth.get_credentials()
    if not creds:
        return None
    return GmailClient(creds)


def get_analyzer():
    """Get or create email analyzer."""
    client = get_gmail_client()
    if not client:
        return None
    return EmailAnalyzer(client)


@app.route('/')
def index():
    """Dashboard home page."""
    if not _auth.has_credentials_file():
        return render_template('setup.html')

    if not _auth.is_authenticated():
        return redirect(url_for('login'))

    client = get_gmail_client()
    profile = None
    try:
        profile = client.get_profile()
    except Exception as e:
        return render_template('error.html', error=str(e))

    return render_template('index.html', profile=profile)


@app.route('/login')
def login():
    """Start OAuth login flow."""
    if not _auth.has_credentials_file():
        return render_template('setup.html')

    redirect_uri = url_for('oauth_callback', _external=True)
    auth_url, state = _auth.get_authorization_url(redirect_uri)
    session['oauth_state'] = state
    return redirect(auth_url)


@app.route('/oauth/callback')
def oauth_callback():
    """Handle OAuth callback."""
    redirect_uri = url_for('oauth_callback', _external=True)

    try:
        _auth.complete_authentication(request.url, redirect_uri)
    except Exception as e:
        return render_template('error.html', error=f"Authentication failed: {e}")

    return redirect(url_for('index'))


@app.route('/logout')
def logout():
    """Log out and clear credentials."""
    _auth.logout()
    session.clear()
    return redirect(url_for('index'))


@app.route('/analyze')
def analyze():
    """Show pattern analysis page."""
    if not _auth.is_authenticated():
        return redirect(url_for('login'))

    return render_template('patterns.html')


@app.route('/api/analyze', methods=['POST'])
def api_analyze():
    """API endpoint to perform email analysis."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    analyzer = get_analyzer()
    if not analyzer:
        return jsonify({'error': 'Failed to create analyzer'}), 500

    query = request.json.get('query', '')
    max_emails = request.json.get('max_emails', config.MAX_EMAILS_TO_ANALYZE)

    try:
        results = analyzer.fetch_and_analyze(
            query=query,
            max_emails=max_emails
        )
        # Store in server-side cache (not session - too large for cookies)
        _analysis_cache['last_analysis'] = results
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/analyze/start', methods=['POST'])
def api_analyze_start():
    """Start email analysis as a background job."""
    global _analysis_job

    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    if _analysis_job['running']:
        return jsonify({'error': 'Analysis already running'}), 400

    data = request.get_json() or {}
    query = data.get('query', '')

    # Reset job state
    _analysis_job = {
        'running': True,
        'progress': {'stage': 'starting', 'message': 'Starting analysis...', 'current': 0, 'total': 0},
        'result': None,
        'error': None
    }

    # Run analysis in background thread
    import threading

    def run_analysis():
        global _analysis_job, _analysis_cache
        try:
            analyzer = get_analyzer()
            if not analyzer:
                _analysis_job['error'] = 'Failed to create analyzer'
                _analysis_job['running'] = False
                return

            client = analyzer.client
            analyzer._emails = []
            analyzer._analysis_cache = None

            # Stage 1: Fetch message IDs
            _analysis_job['progress'] = {'stage': 'fetching_ids', 'message': 'Fetching email IDs...', 'current': 0, 'total': 0}

            full_query = query if query else ''
            if 'in:trash' not in full_query.lower():
                full_query = f'{full_query} -in:trash'.strip()

            print(f"[Analyze] Query: '{full_query}', max_emails: unlimited", flush=True)

            message_ids = []
            for msg in client.fetch_all_messages(query=full_query, max_total=None):
                message_ids.append(msg['id'])
                if len(message_ids) % 500 == 0:
                    _analysis_job['progress'] = {
                        'stage': 'fetching_ids',
                        'message': f'Found {len(message_ids):,} emails...',
                        'current': len(message_ids),
                        'total': 0
                    }

            total_emails = len(message_ids)
            if total_emails == 0:
                result = analyzer._empty_analysis()
                _analysis_cache['last_analysis'] = result
                _analysis_job['result'] = result
                _analysis_job['running'] = False
                return

            _analysis_job['progress'] = {
                'stage': 'fetching_ids',
                'message': f'Found {total_emails:,} emails',
                'current': total_emails,
                'total': total_emails
            }

            # Stage 2: Incremental metadata fetch
            existing_index = load_email_index() or {}
            message_id_set = set(message_ids)

            # Split: known (in enriched index) vs new (need to fetch)
            known_ids = [mid for mid in message_ids
                         if mid in existing_index and existing_index[mid].get('dt')]
            new_ids = [mid for mid in message_ids if mid not in existing_index or not existing_index.get(mid, {}).get('dt')]

            # Reconstruct cached emails
            cached_count = 0
            for mid in known_ids:
                email = analyzer.reconstruct_email_from_index(mid, existing_index[mid])
                if email:
                    analyzer._emails.append(email)
                    cached_count += 1
                else:
                    new_ids.append(mid)

            print(f"[Metadata] {cached_count:,} from cache, {len(new_ids):,} new to fetch", flush=True)
            _analysis_job['progress'] = {
                'stage': 'fetching_metadata',
                'message': f'{cached_count:,} from cache, fetching {len(new_ids):,} new...',
                'current': cached_count,
                'total': total_emails
            }

            # Fetch metadata only for new emails
            metadata_headers = ['From', 'To', 'Subject', 'Date', 'List-Unsubscribe', 'List-Unsubscribe-Post']
            batch_size = 25
            error_count = 0
            success_count = cached_count
            failed_ids = []
            processed_ids = set(known_ids)

            if new_ids:
                print(f"[Metadata] Fetching metadata for {len(new_ids)} new emails in batches of {batch_size}", flush=True)

                for i in range(0, len(new_ids), batch_size):
                    batch_ids = new_ids[i:i + batch_size]

                    max_retries = 3
                    messages = []
                    for attempt in range(max_retries):
                        try:
                            messages = client.get_messages_batch(
                                batch_ids,
                                format='metadata',
                                metadata_headers=metadata_headers
                            )
                            break
                        except Exception as e:
                            if attempt < max_retries - 1:
                                print(f"[Metadata] Batch {i//batch_size + 1} failed (attempt {attempt + 1}): {e}, retrying...", flush=True)
                                time.sleep(2)
                            else:
                                print(f"[Metadata] Batch {i//batch_size + 1} failed after {max_retries} attempts: {e}", flush=True)
                                messages = []

                    batch_errors = 0
                    for msg in messages:
                        if 'error' not in msg:
                            try:
                                analyzer._emails.append(analyzer._parse_message(msg))
                                success_count += 1
                                processed_ids.add(msg.get('id'))
                            except Exception as e:
                                batch_errors += 1
                                failed_ids.append(msg.get('id'))
                        else:
                            batch_errors += 1
                            failed_ids.append(msg.get('id'))

                    error_count += batch_errors
                    if batch_errors > 0:
                        print(f"[Metadata] Batch {i//batch_size + 1}: {len(messages) - batch_errors} success, {batch_errors} errors", flush=True)

                    fetched = cached_count + min(i + batch_size, len(new_ids))
                    _analysis_job['progress'] = {
                        'stage': 'fetching_metadata',
                        'message': f'Processed {fetched:,} of {total_emails:,} ({cached_count:,} cached, {success_count - cached_count:,} fetched, {error_count:,} errors)',
                        'current': fetched,
                        'total': total_emails
                    }

                    time.sleep(0.05)

                print(f"[Metadata] Initial pass complete: {success_count} success ({cached_count} cached), {error_count} errors", flush=True)

            # Retry failed IDs
            if failed_ids:
                for retry_pass in range(3):
                    if not failed_ids:
                        break

                    _analysis_job['progress'] = {
                        'stage': 'retrying',
                        'message': f'Retry pass {retry_pass + 1}: {len(failed_ids):,} failed emails...',
                        'current': 0,
                        'total': len(failed_ids)
                    }
                    print(f"[Retry] Pass {retry_pass + 1}: Retrying {len(failed_ids)} failed emails individually", flush=True)

                    still_failed = []
                    retry_success = 0

                    for idx, msg_id in enumerate(failed_ids):
                        if msg_id in processed_ids:
                            continue

                        try:
                            msg = client.get_message(msg_id, format='metadata', metadata_headers=metadata_headers)
                            analyzer._emails.append(analyzer._parse_message(msg))
                            success_count += 1
                            error_count -= 1
                            retry_success += 1
                            processed_ids.add(msg_id)
                        except Exception as e:
                            still_failed.append(msg_id)

                        if (idx + 1) % 20 == 0:
                            _analysis_job['progress'] = {
                                'stage': 'retrying',
                                'message': f'Retry pass {retry_pass + 1}: {idx + 1:,} of {len(failed_ids):,} ({retry_success:,} recovered)',
                                'current': idx + 1,
                                'total': len(failed_ids)
                            }

                        time.sleep(0.03)

                    failed_ids = still_failed
                    print(f"[Retry] Pass {retry_pass + 1} complete: {retry_success} recovered, {len(still_failed)} still failed", flush=True)

                    if not still_failed:
                        break
                    time.sleep(1)

            print(f"[Metadata] Final: {success_count} success, {error_count} errors out of {total_emails} total", flush=True)

            # Stage 3: Analyze patterns (with user action learning)
            _analysis_job['progress'] = {'stage': 'analyzing', 'message': 'Analyzing patterns...', 'current': 0, 'total': 0}

            user_actions = load_user_actions()
            unsub_tracking = None
            try:
                unsub_path = str(config.UNSUBSCRIBE_TRACKING_FILE)
                if os.path.exists(unsub_path):
                    with open(unsub_path, 'r') as f:
                        unsub_tracking = json.load(f)
            except Exception:
                pass

            result = analyzer.analyze(user_actions=user_actions, unsubscribe_tracking=unsub_tracking)
            _analysis_cache['last_analysis'] = result
            _analysis_job['result'] = result
            _analysis_job['running'] = False

            # Save to file for persistence
            save_analysis_to_file(result)

            # Build and merge email index (preserve existing entries for emails no longer in mailbox)
            new_index = analyzer.build_email_index()
            # Remove entries for emails that no longer exist in Gmail
            pruned_index = {k: v for k, v in existing_index.items() if k in message_id_set}
            pruned_index.update(new_index)
            save_email_index(pruned_index)

            print(f"[Analyze] Complete!", flush=True)

        except Exception as e:
            print(f"[Analyze] Error: {e}", flush=True)
            import traceback
            traceback.print_exc()
            _analysis_job['error'] = str(e)
            _analysis_job['running'] = False

    thread = threading.Thread(target=run_analysis)
    thread.daemon = True
    thread.start()

    return jsonify({'status': 'started'})


@app.route('/api/analyze/progress')
def api_analyze_progress():
    """Get current analysis progress."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    if _analysis_job['error']:
        return jsonify({
            'status': 'error',
            'error': _analysis_job['error']
        })

    if _analysis_job['result']:
        return jsonify({
            'status': 'complete',
            'data': _analysis_job['result']
        })

    if _analysis_job['running']:
        return jsonify({
            'status': 'running',
            'progress': _analysis_job['progress']
        })

    return jsonify({'status': 'idle'})


@app.route('/api/analyze/reset', methods=['POST'])
def api_analyze_reset():
    """Reset analysis job state."""
    global _analysis_job
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    _analysis_job = {
        'running': False,
        'progress': None,
        'result': None,
        'error': None
    }
    return jsonify({'status': 'reset'})


@app.route('/api/analyze/cached')
def api_analyze_cached():
    """Get cached analysis data if available (always reads from file for latest state)."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    # Always try loading from file first to get the latest state after deletions
    cached = load_analysis_from_file()
    if cached:
        _analysis_cache['last_analysis'] = cached
        return jsonify({
            'status': 'available',
            'data': cached
        })

    # Fall back to memory cache if file doesn't exist
    if _analysis_cache.get('last_analysis'):
        return jsonify({
            'status': 'available',
            'data': _analysis_cache['last_analysis']
        })

    return jsonify({'status': 'none'})


@app.route('/api/delete/start', methods=['POST'])
def api_delete_start():
    """Start deletion as a background job."""
    global _delete_job

    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    if _delete_job['running']:
        return jsonify({'error': 'Deletion already running'}), 400

    data = request.get_json() or {}
    email_ids = data.get('email_ids', [])
    permanent = data.get('permanent', False)

    if not email_ids:
        return jsonify({'error': 'No emails specified'}), 400

    # Reset job state
    _delete_job = {
        'running': True,
        'progress': {'current': 0, 'total': len(email_ids), 'success': 0, 'errors': 0, 'message': 'Starting deletion...'},
        'result': None,
        'error': None,
        'email_ids': email_ids,
        'permanent': permanent
    }

    import threading

    def run_deletion():
        global _delete_job
        try:
            client = get_gmail_client()
            if not client:
                _delete_job['error'] = 'Failed to get Gmail client'
                _delete_job['running'] = False
                return

            total = len(email_ids)
            results = {'success': 0, 'errors': []}
            failed_ids = []  # Track failed IDs for retry
            batch_size = 10  # Smaller batch size for reliability

            print(f"[Delete] Starting deletion of {total} emails", flush=True)

            def delete_single_email(msg_id, is_permanent):
                """Delete a single email with retry logic."""
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        if is_permanent:
                            client.service.users().messages().delete(
                                userId='me', id=msg_id
                            ).execute()
                        else:
                            client.service.users().messages().trash(
                                userId='me', id=msg_id
                            ).execute()
                        return True, None
                    except Exception as e:
                        if attempt < max_retries - 1:
                            time.sleep(0.5 * (attempt + 1))  # Exponential backoff
                        else:
                            return False, str(e)
                return False, "Max retries exceeded"

            def execute_batch_with_retry(batch, batch_ids, batch_results, max_retries=3):
                """Execute batch with retry logic."""
                for attempt in range(max_retries):
                    try:
                        batch.execute()
                        return True
                    except Exception as e:
                        print(f"[Delete] Batch attempt {attempt + 1} failed: {e}", flush=True)
                        if attempt < max_retries - 1:
                            time.sleep(1 * (attempt + 1))  # Exponential backoff
                        else:
                            # All retries failed, mark all as failed for individual retry
                            return False
                return False

            for i in range(0, total, batch_size):
                batch_ids = email_ids[i:i + batch_size]
                batch_results = {'success': 0, 'errors': [], 'processed_ids': set()}

                batch = client.service.new_batch_http_request()

                def make_callback(msg_id, batch_res):
                    def callback(request_id, response, exception):
                        batch_res['processed_ids'].add(msg_id)
                        if exception:
                            batch_res['errors'].append({
                                'id': msg_id,
                                'error': str(exception)
                            })
                        else:
                            batch_res['success'] += 1
                    return callback

                for msg_id in batch_ids:
                    if permanent:
                        batch.add(
                            client.service.users().messages().delete(
                                userId='me', id=msg_id
                            ),
                            callback=make_callback(msg_id, batch_results)
                        )
                    else:
                        batch.add(
                            client.service.users().messages().trash(
                                userId='me', id=msg_id
                            ),
                            callback=make_callback(msg_id, batch_results)
                        )

                batch_success = execute_batch_with_retry(batch, batch_ids, batch_results)

                # Track which IDs were successfully deleted in this batch
                batch_deleted_ids = []

                if not batch_success:
                    # Batch completely failed, try individual emails
                    print(f"[Delete] Batch failed completely, trying individual emails", flush=True)
                    for msg_id in batch_ids:
                        success, error = delete_single_email(msg_id, permanent)
                        if success:
                            batch_results['success'] += 1
                            batch_deleted_ids.append(msg_id)
                        else:
                            batch_results['errors'].append({'id': msg_id, 'error': error})
                        time.sleep(0.2)  # Rate limit protection
                else:
                    # Batch succeeded - find which IDs were successful
                    failed_in_batch = set(e['id'] for e in batch_results['errors'])
                    batch_deleted_ids = [mid for mid in batch_ids if mid not in failed_in_batch]

                results['success'] += batch_results['success']
                # Track failed IDs for later retry
                for err in batch_results['errors']:
                    failed_ids.append(err['id'])

                # Progressively update cache after each batch
                if batch_deleted_ids:
                    _update_cached_analysis(batch_deleted_ids, save_to_file=True)
                    _record_deleted_senders(batch_deleted_ids)

                processed = min(i + batch_size, total)
                _delete_job['progress'] = {
                    'current': processed,
                    'total': total,
                    'success': results['success'],
                    'errors': len(failed_ids),
                    'message': f"Deleted {results['success']:,} of {total:,} emails..."
                }

                if batch_results['errors']:
                    print(f"[Delete] Batch {i//batch_size + 1}: {batch_results['success']} success, {len(batch_results['errors'])} errors", flush=True)

                time.sleep(0.3)  # Rate limiting between batches

            # Retry pass: try failed emails individually
            if failed_ids:
                print(f"[Delete] Retrying {len(failed_ids)} failed emails individually", flush=True)
                _delete_job['progress']['message'] = f"Retrying {len(failed_ids)} failed emails..."

                retry_success = 0
                final_errors = []
                retry_deleted_ids = []

                for idx, msg_id in enumerate(failed_ids):
                    success, error = delete_single_email(msg_id, permanent)
                    if success:
                        retry_success += 1
                        retry_deleted_ids.append(msg_id)
                    else:
                        final_errors.append({'id': msg_id, 'error': error})

                    # Update cache every 20 successful retries
                    if len(retry_deleted_ids) >= 20:
                        _update_cached_analysis(retry_deleted_ids, save_to_file=True)
                        _record_deleted_senders(retry_deleted_ids)
                        retry_deleted_ids = []

                    if (idx + 1) % 10 == 0:
                        _delete_job['progress'] = {
                            'current': total,
                            'total': total,
                            'success': results['success'] + retry_success,
                            'errors': len(failed_ids) - retry_success - idx - 1 + len(final_errors),
                            'message': f"Retrying failed emails... {idx + 1}/{len(failed_ids)}"
                        }
                    time.sleep(0.2)

                # Update cache with any remaining retry successes
                if retry_deleted_ids:
                    _update_cached_analysis(retry_deleted_ids, save_to_file=True)
                    _record_deleted_senders(retry_deleted_ids)

                results['success'] += retry_success
                results['errors'] = final_errors
                print(f"[Delete] Retry pass: {retry_success} recovered, {len(final_errors)} still failed", flush=True)

            _delete_job['result'] = results
            _delete_job['running'] = False
            print(f"[Delete] Complete: {results['success']} success, {len(results['errors'])} errors", flush=True)

        except Exception as e:
            print(f"[Delete] Error: {e}", flush=True)
            import traceback
            traceback.print_exc()
            _delete_job['error'] = str(e)
            _delete_job['running'] = False

    thread = threading.Thread(target=run_deletion)
    thread.daemon = True
    thread.start()

    return jsonify({'status': 'started', 'total': len(email_ids)})


@app.route('/api/delete/progress')
def api_delete_progress():
    """Get current deletion progress."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    if _delete_job['error']:
        return jsonify({
            'status': 'error',
            'error': _delete_job['error'],
            'progress': _delete_job['progress']
        })

    if _delete_job['result']:
        return jsonify({
            'status': 'complete',
            'result': _delete_job['result'],
            'progress': _delete_job['progress']
        })

    if _delete_job['running']:
        return jsonify({
            'status': 'running',
            'progress': _delete_job['progress']
        })

    return jsonify({'status': 'idle'})


@app.route('/api/delete/reset', methods=['POST'])
def api_delete_reset():
    """Reset deletion job state."""
    global _delete_job
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    _delete_job = {
        'running': False,
        'progress': None,
        'result': None,
        'error': None,
        'email_ids': [],
        'permanent': False
    }
    return jsonify({'status': 'reset'})


@app.route('/api/analyze/stream')
def api_analyze_stream():
    """SSE endpoint to perform email analysis with progress updates (legacy)."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    # No limit - analyze all emails
    max_emails = None
    query = request.args.get('query', '')

    def generate():
        analyzer = get_analyzer()
        if not analyzer:
            yield f"data: {json.dumps({'error': 'Failed to create analyzer'})}\n\n"
            return

        def progress_callback(current, total, stage):
            progress_data = {
                'type': 'progress',
                'current': current,
                'total': total,
                'stage': stage
            }
            return f"data: {json.dumps(progress_data)}\n\n"

        # We need to yield progress updates during analysis
        # Since the analyzer uses a callback, we'll use a different approach
        # First, yield that we're starting
        yield f"data: {json.dumps({'type': 'progress', 'current': 0, 'total': 0, 'stage': 'starting'})}\n\n"

        try:
            # Custom progress tracking
            client = analyzer.client
            analyzer._emails = []
            analyzer._analysis_cache = None

            # Stage 1: Fetch message IDs
            yield f"data: {json.dumps({'type': 'progress', 'current': 0, 'total': 0, 'stage': 'fetching_ids', 'message': 'Fetching email IDs...'})}\n\n"

            # Include ALL emails except trash
            full_query = query if query else ''
            if 'in:trash' not in full_query.lower():
                # Exclude only trash - include everything else (inbox, sent, spam, archived, etc.)
                full_query = f'{full_query} -in:trash'.strip()

            print(f"[Analyze] Query: '{full_query}', max_emails: {'unlimited' if max_emails is None else max_emails}", flush=True)

            message_ids = []
            for msg in client.fetch_all_messages(query=full_query, max_total=max_emails):
                message_ids.append(msg['id'])
                if len(message_ids) % 500 == 0:
                    yield f"data: {json.dumps({'type': 'progress', 'current': len(message_ids), 'total': 0, 'stage': 'fetching_ids', 'message': f'Found {len(message_ids):,} emails...'})}\n\n"

            total_emails = len(message_ids)
            if total_emails == 0:
                result = analyzer._empty_analysis()
                _analysis_cache['last_analysis'] = result
                yield f"data: {json.dumps({'type': 'complete', 'data': result})}\n\n"
                return

            yield f"data: {json.dumps({'type': 'progress', 'current': total_emails, 'total': total_emails, 'stage': 'fetching_ids', 'message': f'Found {total_emails:,} emails'})}\n\n"

            # Stage 2: Fetch message metadata in batches
            yield f"data: {json.dumps({'type': 'progress', 'current': 0, 'total': total_emails, 'stage': 'fetching_metadata', 'message': 'Fetching email details...'})}\n\n"

            metadata_headers = ['From', 'To', 'Subject', 'Date', 'List-Unsubscribe', 'List-Unsubscribe-Post']
            batch_size = 25  # Small batch size to avoid rate limiting
            error_count = 0
            success_count = 0
            failed_ids = []  # Track all failed IDs for retry
            processed_ids = set()  # Track successfully processed IDs

            print(f"[Metadata] Starting metadata fetch for {total_emails} emails in batches of {batch_size}", flush=True)

            last_update_time = time.time()
            for i in range(0, len(message_ids), batch_size):
                batch_ids = message_ids[i:i + batch_size]

                # Retry logic for failed batches
                max_retries = 3
                messages = []
                for attempt in range(max_retries):
                    try:
                        messages = client.get_messages_batch(
                            batch_ids,
                            format='metadata',
                            metadata_headers=metadata_headers
                        )
                        break
                    except Exception as e:
                        if attempt < max_retries - 1:
                            print(f"[Metadata] Batch {i//batch_size + 1} failed (attempt {attempt + 1}): {e}, retrying...", flush=True)
                            time.sleep(2)
                            # Send keep-alive during retry wait
                            yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"
                        else:
                            print(f"[Metadata] Batch {i//batch_size + 1} failed after {max_retries} attempts: {e}", flush=True)
                            messages = []

                batch_errors = 0
                for msg in messages:
                    if 'error' not in msg:
                        try:
                            analyzer._emails.append(analyzer._parse_message(msg))
                            success_count += 1
                            processed_ids.add(msg.get('id'))
                        except Exception as e:
                            batch_errors += 1
                            failed_ids.append(msg.get('id'))
                    else:
                        batch_errors += 1
                        failed_ids.append(msg.get('id'))

                error_count += batch_errors
                if batch_errors > 0:
                    print(f"[Metadata] Batch {i//batch_size + 1}: {len(messages) - batch_errors} success, {batch_errors} errors", flush=True)

                fetched = min(i + batch_size, total_emails)
                percent = int((fetched / total_emails) * 100)

                # Always send progress update to keep connection alive
                yield f"data: {json.dumps({'type': 'progress', 'current': fetched, 'total': total_emails, 'stage': 'fetching_metadata', 'message': f'Processed {fetched:,} of {total_emails:,} ({success_count:,} loaded, {error_count:,} errors)'})}\n\n"
                last_update_time = time.time()

                # Small delay to avoid rate limiting
                if i + batch_size < len(message_ids):
                    time.sleep(0.05)

            print(f"[Metadata] Initial pass complete: {success_count} success, {error_count} errors", flush=True)

            # Retry failed IDs individually (up to 3 passes)
            if failed_ids:
                for retry_pass in range(3):
                    if not failed_ids:
                        break

                    yield f"data: {json.dumps({'type': 'progress', 'current': 0, 'total': len(failed_ids), 'stage': 'retrying', 'message': f'Retry pass {retry_pass + 1}: {len(failed_ids):,} failed emails...'})}\n\n"
                    print(f"[Retry] Pass {retry_pass + 1}: Retrying {len(failed_ids)} failed emails individually", flush=True)

                    still_failed = []
                    retry_success = 0

                    for idx, msg_id in enumerate(failed_ids):
                        if msg_id in processed_ids:
                            continue  # Already got this one

                        try:
                            msg = client.get_message(msg_id, format='metadata', metadata_headers=metadata_headers)
                            analyzer._emails.append(analyzer._parse_message(msg))
                            success_count += 1
                            error_count -= 1
                            retry_success += 1
                            processed_ids.add(msg_id)
                        except Exception as e:
                            still_failed.append(msg_id)

                        # Progress update every 20 emails to keep connection alive
                        if (idx + 1) % 20 == 0:
                            yield f"data: {json.dumps({'type': 'progress', 'current': idx + 1, 'total': len(failed_ids), 'stage': 'retrying', 'message': f'Retry pass {retry_pass + 1}: {idx + 1:,} of {len(failed_ids):,} ({retry_success:,} recovered)'})}\n\n"

                        time.sleep(0.03)  # Small delay between individual requests

                    failed_ids = still_failed
                    print(f"[Retry] Pass {retry_pass + 1} complete: {retry_success} recovered, {len(still_failed)} still failed", flush=True)

                    if not still_failed:
                        break

                    time.sleep(1)  # Pause between retry passes

            print(f"[Metadata] Final: {success_count} success, {error_count} errors out of {total_emails} total", flush=True)

            # Stage 3: Analyze patterns
            yield f"data: {json.dumps({'type': 'progress', 'current': 0, 'total': 0, 'stage': 'analyzing', 'message': 'Analyzing patterns...'})}\n\n"

            result = analyzer.analyze()
            _analysis_cache['last_analysis'] = result

            # Build and save email index for rule matching
            email_index = analyzer.build_email_index()
            save_email_index(email_index)

            yield f"data: {json.dumps({'type': 'complete', 'data': result})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return Response(generate(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no'
    })


@app.route('/api/pattern/<pattern_type>/<pattern_key>/samples')
def api_pattern_samples(pattern_type, pattern_key):
    """Get sample emails for a pattern."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    analysis = _analysis_cache.get('last_analysis')
    if not analysis:
        return jsonify({'error': 'No analysis data. Please analyze first.'}), 400

    # Find the pattern
    patterns = analysis.get('patterns', {})
    email_ids = []

    for category in patterns.values():
        for pattern in category:
            if pattern['type'] == pattern_type and pattern['key'] == pattern_key:
                email_ids = pattern['email_ids']
                break
        if email_ids:
            break

    if not email_ids:
        return jsonify({'error': 'Pattern not found'}), 404

    analyzer = get_analyzer()
    samples = analyzer.get_email_samples(email_ids, limit=10)

    return jsonify({
        'pattern': {'type': pattern_type, 'key': pattern_key},
        'total': len(email_ids),
        'samples': samples
    })


@app.route('/api/pattern/<pattern_type>/<path:pattern_key>/emails')
def api_pattern_emails(pattern_type, pattern_key):
    """Get paginated emails for a pattern."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    analysis = _analysis_cache.get('last_analysis')
    if not analysis:
        return jsonify({'error': 'No analysis data. Please analyze first.'}), 400

    # Pagination params
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    per_page = min(per_page, 100)  # Max 100 per page

    # Find the pattern
    patterns = analysis.get('patterns', {})
    email_ids = []
    pattern_info = None

    for category in patterns.values():
        for pattern in category:
            if pattern['type'] == pattern_type and pattern['key'] == pattern_key:
                email_ids = pattern['email_ids']
                pattern_info = {
                    'type': pattern['type'],
                    'key': pattern['key'],
                    'display': pattern['display'],
                    'count': pattern['count'],
                    'size_bytes': pattern['size_bytes'],
                }
                break
        if email_ids:
            break

    if not email_ids:
        return jsonify({'error': 'Pattern not found'}), 404

    # Paginate
    total = len(email_ids)
    start = (page - 1) * per_page
    end = start + per_page
    page_ids = email_ids[start:end]

    # Fetch email details
    analyzer = get_analyzer()
    emails = analyzer.get_email_samples(page_ids, limit=per_page)

    return jsonify({
        'pattern': pattern_info,
        'emails': emails,
        'pagination': {
            'page': page,
            'per_page': per_page,
            'total': total,
            'total_pages': (total + per_page - 1) // per_page,
            'has_next': end < total,
            'has_prev': page > 1,
        }
    })


@app.route('/api/email/<email_id>')
def api_email_content(email_id):
    """Get full email content."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    client = get_gmail_client()
    if not client:
        return jsonify({'error': 'Failed to get Gmail client'}), 500

    try:
        email = client.get_message_full(email_id)
        return jsonify(email)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/unsubscribe/<path:sender_key>')
def api_unsubscribe_link(sender_key):
    """Get unsubscribe link for a sender/pattern."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    client = get_gmail_client()
    if not client:
        return jsonify({'error': 'Failed to get Gmail client'}), 500

    try:
        # The sender_key could be an email address or domain
        # Try to find unsubscribe link
        unsub_info = client.get_unsubscribe_link_for_sender(sender_key)

        if unsub_info:
            return jsonify({
                'found': True,
                'type': unsub_info.get('type'),
                'url': unsub_info.get('url'),
                'mailto': unsub_info.get('mailto'),
                'text': unsub_info.get('text'),
                'sender': sender_key
            })
        else:
            return jsonify({
                'found': False,
                'sender': sender_key,
                'message': 'No unsubscribe link found in recent emails from this sender'
            })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/unsubscribe/batch', methods=['POST'])
def api_unsubscribe_batch():
    """Get unsubscribe links for multiple senders."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    client = get_gmail_client()
    if not client:
        return jsonify({'error': 'Failed to get Gmail client'}), 500

    data = request.get_json() or {}
    senders = data.get('senders', [])

    if not senders:
        return jsonify({'error': 'No senders specified'}), 400

    results = []
    for sender in senders[:20]:  # Limit to 20 senders
        try:
            unsub_info = client.get_unsubscribe_link_for_sender(sender)
            if unsub_info:
                results.append({
                    'sender': sender,
                    'found': True,
                    'type': unsub_info.get('type'),
                    'url': unsub_info.get('url'),
                    'mailto': unsub_info.get('mailto'),
                    'text': unsub_info.get('text')
                })
            else:
                results.append({
                    'sender': sender,
                    'found': False
                })
        except Exception as e:
            results.append({
                'sender': sender,
                'found': False,
                'error': str(e)
            })

    return jsonify({'results': results})


@app.route('/confirm')
def confirm():
    """Deletion confirmation page."""
    if not _auth.is_authenticated():
        return redirect(url_for('login'))

    return render_template('confirm.html')


@app.route('/api/delete', methods=['POST'])
def api_delete():
    """API endpoint to move emails to trash."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    email_ids = request.json.get('email_ids', [])
    permanent = request.json.get('permanent', False)

    if not email_ids:
        return jsonify({'error': 'No emails specified'}), 400

    client = get_gmail_client()
    if not client:
        return jsonify({'error': 'Failed to get Gmail client'}), 500

    try:
        if permanent:
            results = client.delete_messages_permanently_batch(email_ids)
        else:
            results = client.trash_messages_batch(email_ids)

        # Log errors for debugging
        if results['errors']:
            import sys
            print(f"\n[Delete] {len(results['errors'])} errors:", flush=True)
            for err in results['errors'][:5]:  # Show first 5
                print(f"  - {err['id']}: {err['error']}", flush=True)
            if len(results['errors']) > 5:
                print(f"  ... and {len(results['errors']) - 5} more", flush=True)
            sys.stdout.flush()

        # Update cached analysis to remove deleted emails
        if results['success'] > 0:
            deleted_ids = set(email_ids) - set(e['id'] for e in results['errors'])
            _update_cached_analysis(deleted_ids)

        return jsonify({
            'success': results['success'],
            'errors': results['errors'],
            'total': len(email_ids)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/delete/stream', methods=['POST'])
def api_delete_stream():
    """SSE endpoint to delete emails with progress updates."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    data = request.get_json()
    email_ids = data.get('email_ids', [])
    permanent = data.get('permanent', False)

    if not email_ids:
        return jsonify({'error': 'No emails specified'}), 400

    def generate():
        import sys

        client = get_gmail_client()
        if not client:
            yield f"data: {json.dumps({'type': 'error', 'error': 'Failed to get Gmail client'})}\n\n"
            return

        total = len(email_ids)
        yield f"data: {json.dumps({'type': 'progress', 'current': 0, 'total': total, 'message': f'Starting deletion of {total:,} emails...'})}\n\n"

        results = {'success': 0, 'errors': []}
        # Use smaller batch size to avoid rate limiting
        batch_size = 20

        print(f"\n[Delete Stream] Starting deletion of {total} emails", flush=True)

        for i in range(0, total, batch_size):
            batch_ids = email_ids[i:i + batch_size]
            batch_results = {'success': 0, 'errors': []}

            batch = client.service.new_batch_http_request()

            def make_callback(msg_id, batch_res):
                def callback(request_id, response, exception):
                    if exception:
                        batch_res['errors'].append({
                            'id': msg_id,
                            'error': str(exception)
                        })
                    else:
                        batch_res['success'] += 1
                return callback

            for msg_id in batch_ids:
                if permanent:
                    batch.add(
                        client.service.users().messages().delete(
                            userId='me', id=msg_id
                        ),
                        callback=make_callback(msg_id, batch_results)
                    )
                else:
                    batch.add(
                        client.service.users().messages().trash(
                            userId='me', id=msg_id
                        ),
                        callback=make_callback(msg_id, batch_results)
                    )

            try:
                batch.execute()
            except Exception as e:
                print(f"[Delete Stream] Batch execute error: {e}", file=sys.stderr, flush=True)
                # Mark all in this batch as errors
                for msg_id in batch_ids:
                    batch_results['errors'].append({
                        'id': msg_id,
                        'error': f'Batch failed: {str(e)}'
                    })
                batch_results['success'] = 0

            # Aggregate results
            results['success'] += batch_results['success']
            results['errors'].extend(batch_results['errors'])

            # Log batch results
            if batch_results['errors']:
                print(f"[Delete Stream] Batch {i//batch_size + 1}: {batch_results['success']} success, {len(batch_results['errors'])} errors", flush=True)
                for err in batch_results['errors'][:3]:
                    print(f"  - {err['id']}: {err['error'][:100]}", flush=True)

            processed = min(i + batch_size, total)
            percent = int((processed / total) * 100)
            success_count = results['success']
            error_count = len(results['errors'])
            msg = f'Deleted {success_count:,} of {total:,} emails ({percent}%) - {error_count} errors'
            progress_data = {
                'type': 'progress',
                'current': processed,
                'total': total,
                'success': success_count,
                'errors': error_count,
                'message': msg
            }
            yield f"data: {json.dumps(progress_data)}\n\n"

            # Longer delay to avoid rate limiting
            if i + batch_size < total:
                time.sleep(1.0)

        # Update cached analysis to remove deleted emails
        if results['success'] > 0:
            deleted_ids = set(email_ids) - set(e['id'] for e in results['errors'])
            _update_cached_analysis(deleted_ids)

        print(f"[Delete Stream] Complete: {results['success']} success, {len(results['errors'])} errors", flush=True)

        yield f"data: {json.dumps({'type': 'complete', 'success': results['success'], 'errors': results['errors'], 'total': total})}\n\n"

    return Response(generate(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no'
    })


@app.route('/trash')
def trash():
    """Trash review page."""
    if not _auth.is_authenticated():
        return redirect(url_for('login'))

    return render_template('trash.html')


@app.route('/api/trash/analyze', methods=['POST'])
def api_analyze_trash():
    """Analyze trash contents."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    analyzer = get_analyzer()
    if not analyzer:
        return jsonify({'error': 'Failed to create analyzer'}), 500

    max_emails = request.json.get('max_emails', 1000)

    try:
        results = analyzer.fetch_and_analyze(
            label_ids=['TRASH'],
            max_emails=max_emails
        )
        _analysis_cache['trash_analysis'] = results
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/analysis/cached')
def api_cached_analysis():
    """Get cached analysis data if available."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    analysis = _analysis_cache.get('last_analysis')
    if not analysis:
        return jsonify({'cached': False})

    return jsonify({'cached': True, 'data': analysis})


@app.route('/api/profile')
def api_profile():
    """Get user profile info."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    client = get_gmail_client()
    try:
        profile = client.get_profile()
        return jsonify(profile)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def format_size(size_bytes):
    """Format bytes as human-readable size."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


# Register template filters
app.jinja_env.filters['format_size'] = format_size


# ============================================================
# Rules Routes
# ============================================================

_rule_engine = RuleEngine()


@app.route('/rules')
def rules_page():
    """Rules management page."""
    if not _auth.is_authenticated():
        return redirect(url_for('login'))
    return render_template('rules.html')


@app.route('/api/rules')
def api_rules_list():
    """List all rules."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401
    return jsonify({'rules': _rule_engine.load_rules()})


@app.route('/api/rules', methods=['POST'])
def api_rules_create():
    """Create a new rule."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    data = request.get_json() or {}
    name = data.get('name', '').strip()
    conditions = data.get('conditions', [])
    action = data.get('action', 'trash')
    description = data.get('description', '')
    condition_logic = data.get('condition_logic', 'AND')

    if not name:
        return jsonify({'error': 'Rule name is required'}), 400
    if not conditions:
        return jsonify({'error': 'At least one condition is required'}), 400
    if action not in ('trash', 'delete'):
        return jsonify({'error': 'Action must be trash or delete'}), 400

    rule = _rule_engine.create_rule(name, conditions, action, description, condition_logic)
    return jsonify({'rule': rule})


@app.route('/api/rules/<rule_id>', methods=['PUT'])
def api_rules_update(rule_id):
    """Update an existing rule."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    data = request.get_json() or {}
    allowed_fields = {'name', 'description', 'conditions', 'action', 'condition_logic', 'enabled'}
    updates = {k: v for k, v in data.items() if k in allowed_fields}

    rule = _rule_engine.update_rule(rule_id, **updates)
    if rule:
        return jsonify({'rule': rule})
    return jsonify({'error': 'Rule not found'}), 404


@app.route('/api/rules/<rule_id>', methods=['DELETE'])
def api_rules_delete(rule_id):
    """Delete a rule."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    if _rule_engine.delete_rule(rule_id):
        return jsonify({'status': 'deleted'})
    return jsonify({'error': 'Rule not found'}), 404


@app.route('/api/rules/<rule_id>/preview', methods=['POST'])
def api_rules_preview(rule_id):
    """Preview which emails match a rule."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    rule = _rule_engine.get_rule(rule_id)
    if not rule:
        return jsonify({'error': 'Rule not found'}), 404

    email_index = load_email_index()
    if not email_index:
        return jsonify({'error': 'No email index. Please run analysis first.'}), 400

    preview = _rule_engine.preview_rule(rule, email_index)

    # Get sample emails for display
    samples = []
    if preview['matched_ids']:
        analyzer = get_analyzer()
        if analyzer:
            samples = analyzer.get_email_samples(preview['matched_ids'][:10], limit=10)

    return jsonify({
        'matched_count': preview['matched_count'],
        'estimated_size_bytes': preview['estimated_size_bytes'],
        'samples': samples,
    })


@app.route('/api/rules/<rule_id>/execute', methods=['POST'])
def api_rules_execute(rule_id):
    """Execute a rule — delete matching emails."""
    global _rule_job

    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    if _rule_job['running'] or _delete_job['running']:
        return jsonify({'error': 'Another job is already running'}), 400

    rule = _rule_engine.get_rule(rule_id)
    if not rule:
        return jsonify({'error': 'Rule not found'}), 404

    email_index = load_email_index()
    if not email_index:
        return jsonify({'error': 'No email index. Please run analysis first.'}), 400

    matched_ids = _rule_engine.match_emails(rule, email_index)
    if not matched_ids:
        return jsonify({'error': 'No emails match this rule'}), 400

    _rule_job = {
        'running': True,
        'progress': {'current': 0, 'total': len(matched_ids), 'success': 0, 'errors': 0, 'message': 'Starting...'},
        'result': None,
        'error': None,
        'rule_id': rule_id,
    }

    import threading

    def run_rule_execution():
        global _rule_job
        try:
            client = get_gmail_client()
            if not client:
                _rule_job['error'] = 'Failed to get Gmail client'
                _rule_job['running'] = False
                return

            total = len(matched_ids)
            action = rule.get('action', 'trash')
            permanent = action == 'delete'
            results = {'success': 0, 'errors': []}
            batch_size = 10

            print(f"[Rule] Executing rule '{rule['name']}': {action} {total} emails", flush=True)

            for i in range(0, total, batch_size):
                batch_ids = matched_ids[i:i + batch_size]

                for msg_id in batch_ids:
                    try:
                        if permanent:
                            client.service.users().messages().delete(userId='me', id=msg_id).execute()
                        else:
                            client.service.users().messages().trash(userId='me', id=msg_id).execute()
                        results['success'] += 1
                    except Exception as e:
                        results['errors'].append({'id': msg_id, 'error': str(e)})

                if results['success'] > 0:
                    successful_ids = [mid for mid in batch_ids if mid not in {e['id'] for e in results['errors']}]
                    if successful_ids:
                        _update_cached_analysis(successful_ids, save_to_file=True)

                processed = min(i + batch_size, total)
                _rule_job['progress'] = {
                    'current': processed,
                    'total': total,
                    'success': results['success'],
                    'errors': len(results['errors']),
                    'message': f"Processed {results['success']:,} of {total:,} emails...",
                }
                time.sleep(0.2)

            # Update rule with last run info
            _rule_engine.update_rule(rule_id, last_run=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                                     last_run_result={'success': results['success'], 'errors': len(results['errors'])})

            _rule_job['result'] = results
            _rule_job['running'] = False
            print(f"[Rule] Complete: {results['success']} success, {len(results['errors'])} errors", flush=True)

        except Exception as e:
            print(f"[Rule] Error: {e}", flush=True)
            import traceback
            traceback.print_exc()
            _rule_job['error'] = str(e)
            _rule_job['running'] = False

    thread = threading.Thread(target=run_rule_execution)
    thread.daemon = True
    thread.start()

    return jsonify({'status': 'started', 'total': len(matched_ids)})


@app.route('/api/rules/execute/progress')
def api_rules_progress():
    """Get rule execution progress."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    if _rule_job['error']:
        return jsonify({'status': 'error', 'error': _rule_job['error'], 'progress': _rule_job['progress']})
    if _rule_job['result']:
        return jsonify({'status': 'complete', 'result': _rule_job['result'], 'progress': _rule_job['progress']})
    if _rule_job['running']:
        return jsonify({'status': 'running', 'progress': _rule_job['progress']})
    return jsonify({'status': 'idle'})


@app.route('/api/rules/execute/reset', methods=['POST'])
def api_rules_reset():
    """Reset rule execution job state."""
    global _rule_job
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401
    _rule_job = {'running': False, 'progress': None, 'result': None, 'error': None, 'rule_id': None}
    return jsonify({'status': 'reset'})


# ============================================================
# Unsubscribe Manager Routes
# ============================================================

@app.route('/unsubscribe')
def unsubscribe_page():
    """Unsubscribe manager page."""
    if not _auth.is_authenticated():
        return redirect(url_for('login'))
    return render_template('unsubscribe.html')


@app.route('/api/unsubscribe/suggestions')
def api_unsub_suggestions():
    """Get unsubscribe suggestions based on analysis data."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    analysis = _analysis_cache.get('last_analysis')
    if not analysis:
        analysis = load_analysis_from_file()
        if analysis:
            _analysis_cache['last_analysis'] = analysis

    if not analysis:
        return jsonify({'error': 'No analysis data. Please run analysis first.'}), 400

    email_index = load_email_index()
    if not email_index:
        return jsonify({'error': 'No email index. Please run analysis first.'}), 400

    client = get_gmail_client()
    if not client:
        return jsonify({'error': 'Failed to get Gmail client'}), 500

    manager = UnsubscribeManager(client)
    suggestions = manager.get_suggestions(analysis, email_index)

    return jsonify({
        'suggestions': suggestions,
        'already_unsubscribed': len(manager.get_unsubscribed_senders()),
    })


@app.route('/api/unsubscribe/execute', methods=['POST'])
def api_unsub_execute():
    """Unsubscribe from a single sender."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    client = get_gmail_client()
    if not client:
        return jsonify({'error': 'Failed to get Gmail client'}), 500

    data = request.get_json() or {}
    sender_email = data.get('sender_email', '')
    display_name = data.get('display_name', '')

    if not sender_email:
        return jsonify({'error': 'sender_email is required'}), 400

    # Fetch fresh unsubscribe info
    unsub_info = client.get_unsubscribe_link_for_sender(sender_email)
    if not unsub_info:
        return jsonify({'error': f'No unsubscribe link found for {sender_email}'}), 404

    # Convert to the format UnsubscribeManager expects
    full_msg = client.get_message_full(unsub_info['source_email_id'])
    unsub_links = full_msg.get('unsubscribe_links', {})

    manager = UnsubscribeManager(client)
    result = manager.execute_unsubscribe(sender_email, unsub_links, display_name)

    return jsonify(result)


@app.route('/api/unsubscribe/batch/start', methods=['POST'])
def api_unsub_batch_start():
    """Start batch unsubscribe as a background job."""
    global _unsubscribe_job

    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    if _unsubscribe_job['running']:
        return jsonify({'error': 'Unsubscribe job already running'}), 400

    data = request.get_json() or {}
    senders = data.get('senders', [])

    if not senders:
        return jsonify({'error': 'No senders specified'}), 400

    _unsubscribe_job = {
        'running': True,
        'progress': {'current': 0, 'total': len(senders), 'message': 'Starting...', 'details': []},
        'result': None,
        'error': None,
    }

    import threading

    def run_batch_unsub():
        global _unsubscribe_job
        try:
            client = get_gmail_client()
            if not client:
                _unsubscribe_job['error'] = 'Failed to get Gmail client'
                _unsubscribe_job['running'] = False
                return

            manager = UnsubscribeManager(client)
            total = len(senders)
            results = {'success': 0, 'failed': 0, 'details': []}

            for i, sender_info in enumerate(senders):
                sender_email = sender_info.get('sender_email', '')
                display_name = sender_info.get('display_name', '')

                _unsubscribe_job['progress']['message'] = f"Unsubscribing from {sender_email}..."

                # Fetch fresh unsubscribe info
                unsub_info_raw = client.get_unsubscribe_link_for_sender(sender_email)

                if unsub_info_raw:
                    full_msg = client.get_message_full(unsub_info_raw['source_email_id'])
                    unsub_links = full_msg.get('unsubscribe_links', {})
                    result = manager.execute_unsubscribe(sender_email, unsub_links, display_name)
                else:
                    result = {
                        'sender_email': sender_email,
                        'status': 'failed',
                        'error': 'No unsubscribe link found',
                    }

                if result.get('status') in ('success', 'unknown'):
                    results['success'] += 1
                else:
                    results['failed'] += 1
                results['details'].append(result)

                _unsubscribe_job['progress'] = {
                    'current': i + 1,
                    'total': total,
                    'message': f"Processed {i + 1} of {total}...",
                    'details': results['details'],
                }

                if i + 1 < total:
                    time.sleep(config.UNSUBSCRIBE_BATCH_DELAY)

            _unsubscribe_job['result'] = results
            _unsubscribe_job['running'] = False
            print(f"[Unsub] Batch complete: {results['success']} success, {results['failed']} failed", flush=True)

        except Exception as e:
            print(f"[Unsub] Batch error: {e}", flush=True)
            _unsubscribe_job['error'] = str(e)
            _unsubscribe_job['running'] = False

    thread = threading.Thread(target=run_batch_unsub)
    thread.daemon = True
    thread.start()

    return jsonify({'status': 'started', 'total': len(senders)})


@app.route('/api/unsubscribe/batch/progress')
def api_unsub_batch_progress():
    """Get batch unsubscribe progress."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    if _unsubscribe_job['error']:
        return jsonify({'status': 'error', 'error': _unsubscribe_job['error']})
    if _unsubscribe_job['result']:
        return jsonify({'status': 'complete', 'result': _unsubscribe_job['result']})
    if _unsubscribe_job['running']:
        return jsonify({'status': 'running', 'progress': _unsubscribe_job['progress']})
    return jsonify({'status': 'idle'})


@app.route('/api/unsubscribe/batch/reset', methods=['POST'])
def api_unsub_batch_reset():
    """Reset unsubscribe job state."""
    global _unsubscribe_job
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401
    _unsubscribe_job = {'running': False, 'progress': None, 'result': None, 'error': None}
    return jsonify({'status': 'reset'})


@app.route('/api/unsubscribe/history')
def api_unsub_history():
    """Get unsubscribe history."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    client = get_gmail_client()
    if not client:
        return jsonify({'error': 'Failed to get Gmail client'}), 500

    manager = UnsubscribeManager(client)
    return jsonify({'history': manager.get_unsubscribed_senders()})


# ============================================================
# User Actions Routes
# ============================================================

@app.route('/api/user-actions')
def api_user_actions():
    """Get user action history."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401
    return jsonify(load_user_actions())


@app.route('/api/patterns/<pattern_type>/<path:pattern_key>/dismiss', methods=['POST'])
def api_pattern_dismiss(pattern_type, pattern_key):
    """Record that the user dismissed a pattern."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    actions = load_user_actions()
    action_key = f'{pattern_type}:{pattern_key}'
    existing = actions['dismissed'].get(action_key, {'times_dismissed': 0})
    existing['dismissed_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    existing['times_dismissed'] = existing.get('times_dismissed', 0) + 1
    actions['dismissed'][action_key] = existing
    # Remove from kept if it was there
    actions['kept'].pop(action_key, None)
    save_user_actions(actions)
    return jsonify({'status': 'dismissed', 'key': action_key})


@app.route('/api/patterns/<pattern_type>/<path:pattern_key>/keep', methods=['POST'])
def api_pattern_keep(pattern_type, pattern_key):
    """Record that the user explicitly wants to keep a pattern."""
    if not _auth.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    actions = load_user_actions()
    action_key = f'{pattern_type}:{pattern_key}'
    existing = actions['kept'].get(action_key, {'times_kept': 0})
    existing['kept_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    existing['times_kept'] = existing.get('times_kept', 0) + 1
    actions['kept'][action_key] = existing
    # Remove from dismissed if it was there
    actions['dismissed'].pop(action_key, None)
    save_user_actions(actions)
    return jsonify({'status': 'kept', 'key': action_key})


if __name__ == '__main__':
    print("\n" + "="*60)
    print("Gmail Cleanup App")
    print("="*60)

    if not _auth.has_credentials_file():
        print("\nWARNING: credentials.json not found!")
        print("Please download it from Google Cloud Console:")
        print("1. Go to https://console.cloud.google.com/")
        print("2. Create a project and enable Gmail API")
        print("3. Create OAuth 2.0 credentials (Desktop app)")
        print("4. Download and save as 'credentials.json' in this folder")
        print()

    print(f"\nStarting server at http://127.0.0.1:5000")
    print("Press Ctrl+C to stop\n")

    app.run(debug=config.DEBUG, port=5000)
