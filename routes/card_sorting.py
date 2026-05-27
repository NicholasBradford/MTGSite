import re
from flask import Blueprint, request, redirect, url_for, render_template, flash
from db.db_manager import CardDB
from flask_login import login_required, current_user

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
manager = CardDB()

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

@sorter_bp.route('/sort/<int:source_location_id>', methods=['POST'])
@login_required
def sort_and_relocate_inventory(source_location_id):
    # Security Check: Ensure only the admin can run the sorter script
    if current_user.role != 'admin':
        flash("Unauthorized access.", "danger")
        return redirect(url_for('admin.admin_dashboard'))

    cursor = manager.conn.cursor()
    try:
        # 1. Fetch source dataset 
        query = '''
            SELECT i.instance_id AS inventory_id, cd.name, cd.color, c.collector_number, cd.type_line
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
            cleaned_name = clean_sort_name(item['name'])
            first_letter = cleaned_name[0] if cleaned_name else "A"
            
            if 'Basic Land' in item['type_line']:
                box_name = "Land (Basic)"
            elif 'Land' in item['type_line']:
                box_name = "Land (Util)"
            else:  
                color_prefix = determine_color_prefix(item['color'])
                alpha_range = determine_box_range(first_letter, color_prefix == "C" )
                box_name = f"{color_prefix}-{alpha_range}"
                
            sort_key = (
                box_name,
                cleaned_name.lower(),
                parse_collector_number(item['collector_number'])
            )
                
            processed_items.append({
                'inventory_id': item['inventory_id'],
                'box_name': box_name,
                'sort_key': sort_key
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
        flash(f"Successfully sorted and migrated {updated_count} cards into color/alphabetical boxes!", "success")

    except Exception as e:
        manager.conn.rollback()
        flash(f"Database operation failed: {str(e)}", "danger")
        
    return redirect(url_for('admin.admin_dashboard'))
