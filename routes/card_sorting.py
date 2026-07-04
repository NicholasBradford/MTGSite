import re
from flask import Blueprint, request, redirect, url_for, render_template, flash, current_app
from db.db_manager import get_db
from flask_login import login_required, current_user
from services.feature_flags import require_feature


ALPHABET_BINS = [
    ('A', 'G', 'A:G'),
    ('H', 'R', 'H:R'),
    ('S', 'Z', 'S:Z')
]

COLORLESS_BINS = [
    ('A', 'M', 'A:M'),
    ('N', 'Z', 'N:Z')
]

sorter_bp = Blueprint('sorter', __name__)

def clean_sort_name(name):
    """ Removes leading articles ('A ', 'An ', 'The ') for accurate alphabetical sorting. """
    if not name:
        return ""
    cleaned = re.sub(r'^(The|A|An)\s+', '', name, flags=re.IGNORECASE)
    return cleaned.strip()

def parse_collector_number(col_num):
    """ Parses collector numbers into numerical tuples to safely handle sort index sequencing. """
    if not col_num:
        return (0, "")
    match = re.match(r'^(\d+)(.*)$', str(col_num).strip())
    if match:
        return (int(match.group(1)), match.group(2).lower())
    return (0, str(col_num).lower())

def determine_box_range(first_char, colorless):
    """ Finds which alphabetical bin a character falls into. """
    if not first_char or not first_char.isalpha():
        return "A:G"
    
    char = first_char.upper()
    if not colorless:
        for start, end, label in ALPHABET_BINS:
            if start <= char <= end:
                return label
        return "A:G"
    else:
        for start, end, label in COLORLESS_BINS:
            if start <= char <= end:
                return label
        return "A:M"

def determine_color_prefix(colors_str):
    """ Maps card colors field into specific classification prefix identifiers. """
    if not colors_str or colors_str.strip() == "" or colors_str.upper() == "[]":
        return "C"
    
    clean_colors = colors_str.replace("[", "").replace("]", "").replace('"', '').replace("'", "").strip().upper()
    if "," in clean_colors or (len(clean_colors) > 1 and clean_colors.isalpha()):
        return "M"
    
    color_map = {
        'W': 'W', 'WHITE': 'W',
        'U': 'U', 'BLUE': 'U',
        'B': 'B', 'BLACK': 'B',
        'R': 'R', 'RED': 'R',
        'G': 'G', 'GREEN': 'G'
    }
    return color_map.get(clean_colors, "C")

def get_or_create_location_id(cursor, box_name):
    """ Resolves the targeted Box location record. Inserts it if missing. """
    cursor.execute("SELECT location_id FROM locations WHERE name = ?", (box_name,))
    row = cursor.fetchone()
    if row:
        return row['location_id']
    
    cursor.execute("INSERT INTO locations (name) VALUES (?)", (box_name,))
    return cursor.lastrowid

def get_autosorter_settings():
    """
    Reads autosorter settings from app_settings.json through SettingsManager.

    Expected JSON:
    "autosorter": {
        "enabled": true,
        "strategy": "default"
    }
    """
    manager = current_app.extensions.get("settings_manager")

    if not manager:
        return {
            "enabled": True,
            "strategy": "default",
        }

    return {
        "enabled": manager.get("autosorter", "enabled", True),
        "strategy": str(manager.get("autosorter", "strategy", "default")).strip().lower(),
    }


def get_row_value(row, key, default=""):
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default

    if value is None:
        return default

    return value


def default_sort_profile(item):
    """
    Current behavior:
    - Basic lands -> Land (Basic)
    - Other lands -> Land (Util)
    - Everything else -> color prefix + alphabet range
      Example: W-A:G, U-H:R, C-A:M
    """
    cleaned_name = clean_sort_name(get_row_value(item, "name"))
    first_letter = cleaned_name[0] if cleaned_name else "A"
    type_line = get_row_value(item, "type_line")
    collector_number = get_row_value(item, "collector_number")

    if "Basic Land" in type_line:
        box_name = "Land (Basic)"
    elif "Land" in type_line:
        box_name = "Land (Util)"
    else:
        color_prefix = determine_color_prefix(get_row_value(item, "color"))
        alpha_range = determine_box_range(first_letter, color_prefix == "C")
        box_name = f"{color_prefix}-{alpha_range}"

    sort_key = (
        box_name,
        cleaned_name.lower(),
        parse_collector_number(collector_number),
    )

    return box_name, sort_key


def alphabetical_sort_profile(item):
    """
    Ignores color and sorts by name range only.
    """
    cleaned_name = clean_sort_name(get_row_value(item, "name"))
    first_letter = cleaned_name[0] if cleaned_name else "A"
    collector_number = get_row_value(item, "collector_number")

    alpha_range = determine_box_range(first_letter, colorless=False)
    box_name = f"Name-{alpha_range}"

    sort_key = (
        box_name,
        cleaned_name.lower(),
        parse_collector_number(collector_number),
    )

    return box_name, sort_key


def color_only_sort_profile(item):
    """
    Sorts nonlands into broad color boxes only.
    """
    cleaned_name = clean_sort_name(get_row_value(item, "name"))
    type_line = get_row_value(item, "type_line")
    collector_number = get_row_value(item, "collector_number")

    if "Basic Land" in type_line:
        box_name = "Land (Basic)"
    elif "Land" in type_line:
        box_name = "Land (Util)"
    else:
        color_prefix = determine_color_prefix(get_row_value(item, "color"))
        color_names = {
            "W": "White",
            "U": "Blue",
            "B": "Black",
            "R": "Red",
            "G": "Green",
            "M": "Multicolor",
            "C": "Colorless",
        }
        box_name = color_names.get(color_prefix, "Colorless")

    sort_key = (
        box_name,
        cleaned_name.lower(),
        parse_collector_number(collector_number),
    )

    return box_name, sort_key


def set_sort_profile(item):
    """
    Sorts cards into one location per set.
    Example: Set-MKM, Set-SOS
    """
    cleaned_name = clean_sort_name(get_row_value(item, "name"))
    set_code = get_row_value(item, "set_code", "unknown")
    collector_number = get_row_value(item, "collector_number")

    box_name = f"Set-{str(set_code).upper()}"

    sort_key = (
        box_name,
        parse_collector_number(collector_number),
        cleaned_name.lower(),
    )

    return box_name, sort_key


SORT_STRATEGIES = {
    "default": default_sort_profile,
    "color_alpha": default_sort_profile,
    "color_alphabetical": default_sort_profile,

    "alphabetical": alphabetical_sort_profile,
    "alpha": alphabetical_sort_profile,
    "name": alphabetical_sort_profile,

    "color_only": color_only_sort_profile,
    "color": color_only_sort_profile,

    "set": set_sort_profile,
    "set_code": set_sort_profile,
}

@sorter_bp.route('/sort/<int:source_location_id>', methods=['POST'])
@login_required
def sort_and_relocate_inventory(source_location_id):
    # Security Check: Ensure only the admin can run the sorter script
    if current_user.role != 'admin':
        flash("Unauthorized access.", "danger")
        return redirect(url_for('admin.admin_dashboard'))
    
    autosorter_settings = get_autosorter_settings()

    if not autosorter_settings["enabled"]:
        flash("Autosorter is disabled in app_settings.json.", "warning")
        return redirect(url_for('admin.admin_dashboard'))

    strategy_name = autosorter_settings["strategy"]
    strategy_func = SORT_STRATEGIES.get(strategy_name)

    if not strategy_func:
        flash(
            f"Unknown autosorter strategy '{strategy_name}'. Falling back to default.",
            "warning"
        )
        strategy_name = "default"
        strategy_func = SORT_STRATEGIES["default"]

    manager = get_db()
    cursor = manager.conn.cursor()
    try:
        # 1. Fetch source dataset 
        query = '''
            SELECT
                i.instance_id AS inventory_id,
                cd.name,
                cd.color,
                c.set_code,
                c.collector_number,
                cd.type_line
            FROM inventory i
            JOIN card_printings c ON i.scryfall_id = c.scryfall_id
            JOIN card_definitions cd ON c.oracle_id = cd.oracle_id
            WHERE i.location_id = ?
        '''
        cursor.execute(query, (source_location_id,))
        inventory_items = cursor.fetchall()
        
        if not inventory_items:
            flash(f"No cards found matching location ID {source_location_id}.", "info")
            return redirect(url_for('admin.admin_dashboard'))

        # 2. Assign box routing destination profiles 
        processed_items = []

        for item in inventory_items:
            box_name, sort_key = strategy_func(item)

            processed_items.append({
                'inventory_id': item['inventory_id'],
                'box_name': box_name,
                'sort_key': sort_key,
            })

        # 3. Apply alphabetical layout sort weights
        processed_items.sort(key=lambda x: x['sort_key'])

        # 4. Perform atomic target migrations
        updated_count = 0
        for item in processed_items:
            target_location_id = get_or_create_location_id(cursor, item['box_name'])
            
            cursor.execute('''
                UPDATE inventory 
                SET location_id = ? 
                WHERE instance_id = ?
            ''', (target_location_id, item['inventory_id']))
            updated_count += 1
            
        manager.conn.commit()
        flash(
            f"Successfully sorted and migrated {updated_count} cards using the '{strategy_name}' autosorter strategy!",
            "success"
        )
        
    except Exception as e:
        manager.conn.rollback()
        flash(f"Database operation failed: {str(e)}", "danger")
        
    return redirect(url_for('admin.admin_dashboard'))
