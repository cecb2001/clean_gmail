import json
import time
import random
import string
from pathlib import Path

import config


class RuleEngine:
    """Manages cleanup rules and matches them against an email index."""

    def __init__(self, rules_file=None):
        self.rules_file = rules_file or config.RULES_FILE

    def load_rules(self):
        try:
            if Path(self.rules_file).exists():
                with open(self.rules_file, 'r') as f:
                    data = json.load(f)
                return data.get('rules', [])
        except Exception as e:
            print(f"[Rules] Failed to load: {e}", flush=True)
        return []

    def save_rules(self, rules):
        with open(self.rules_file, 'w') as f:
            json.dump({'rules': rules}, f, indent=2)

    def create_rule(self, name, conditions, action, description='', condition_logic='AND'):
        rules = self.load_rules()
        now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
        rule = {
            'id': f"rule_{int(time.time())}_{suffix}",
            'name': name,
            'description': description,
            'created_at': now,
            'updated_at': now,
            'conditions': conditions,
            'condition_logic': condition_logic,
            'action': action,
            'enabled': True,
            'last_run': None,
            'last_run_result': None,
        }
        rules.append(rule)
        self.save_rules(rules)
        return rule

    def update_rule(self, rule_id, **kwargs):
        rules = self.load_rules()
        for rule in rules:
            if rule['id'] == rule_id:
                for key, value in kwargs.items():
                    if key in rule and key != 'id':
                        rule[key] = value
                rule['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                self.save_rules(rules)
                return rule
        return None

    def delete_rule(self, rule_id):
        rules = self.load_rules()
        original_len = len(rules)
        rules = [r for r in rules if r['id'] != rule_id]
        if len(rules) < original_len:
            self.save_rules(rules)
            return True
        return False

    def get_rule(self, rule_id):
        for rule in self.load_rules():
            if rule['id'] == rule_id:
                return rule
        return None

    # Field mapping: short keys in email index -> condition field names
    FIELD_MAP = {
        'sender_email': 'se',
        'sender_domain': 'sd',
        'age_days': 'ad',
        'size_bytes': 'sb',
        'is_unread': 'ur',
        'has_unsubscribe': 'hu',
        'category': 'lb',  # special: checks label membership
    }

    def match_emails(self, rule, email_index):
        """Match rule conditions against email index. Returns list of matched email IDs."""
        conditions = rule.get('conditions', [])
        logic = rule.get('condition_logic', 'AND')
        matched = []

        for email_id, data in email_index.items():
            results = [self._evaluate_condition(c, data) for c in conditions]
            if logic == 'AND' and all(results):
                matched.append(email_id)
            elif logic == 'OR' and any(results):
                matched.append(email_id)

        return matched

    def _evaluate_condition(self, condition, email_data):
        field = condition.get('field')
        operator = condition.get('operator')
        value = condition.get('value')

        index_key = self.FIELD_MAP.get(field)
        if not index_key:
            return False

        # Category is special: check if value is in the labels list
        if field == 'category':
            labels = email_data.get('lb', [])
            if operator == 'eq':
                return value in labels
            elif operator == 'neq':
                return value not in labels
            return False

        actual = email_data.get(index_key)
        if actual is None:
            return False

        # Boolean fields
        if field in ('is_unread', 'has_unsubscribe'):
            target = value if isinstance(value, bool) else str(value).lower() == 'true'
            if operator == 'eq':
                return actual == target
            elif operator == 'neq':
                return actual != target
            return False

        # Numeric fields
        if field in ('age_days', 'size_bytes'):
            try:
                num_value = float(value)
            except (TypeError, ValueError):
                return False
            if operator == 'eq':
                return actual == num_value
            elif operator == 'neq':
                return actual != num_value
            elif operator == 'gte':
                return actual >= num_value
            elif operator == 'lte':
                return actual <= num_value
            return False

        # String fields (sender_email, sender_domain)
        actual_str = str(actual).lower()
        value_str = str(value).lower()
        if operator == 'eq':
            return actual_str == value_str
        elif operator == 'neq':
            return actual_str != value_str
        elif operator == 'contains':
            return value_str in actual_str
        elif operator == 'in':
            return actual_str in value_str
        return False

    def preview_rule(self, rule, email_index):
        matched_ids = self.match_emails(rule, email_index)
        total_size = sum(
            email_index[eid].get('sb', 0)
            for eid in matched_ids
            if eid in email_index
        )
        return {
            'matched_count': len(matched_ids),
            'matched_ids': matched_ids[:config.MAX_RULE_PREVIEW],
            'estimated_size_bytes': total_size,
        }
