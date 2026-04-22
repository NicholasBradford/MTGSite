from flask import Blueprint, render_template
from db.db_manager import CardDB # Adjust based on your actual file structure

main_bp = Blueprint('main', __name__)

@main_bp.route('/')
def index():
    manager = CardDB()
    
    # 1. Get Random Spotlight Card
    spotlight_query = """
        SELECT cp.image_url, cd.name, cp.set_code, s.set_name
        FROM inventory i
        JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
        JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
        JOIN sets s ON cp.set_code = s.set_code
        ORDER BY RANDOM() LIMIT 1
    """
    spotlight = manager.cursor.execute(spotlight_query).fetchone()

    # 2. Get Recently Added Cards (Last 10)
    # Assumes 'instance_id' increments or you have an 'added' timestamp
    recent_query = """
        SELECT cp.image_url, cd.name 
        FROM inventory i
        JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
        JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
        GROUP BY cp.scryfall_id
        ORDER BY MAX(i.instance_id) DESC 
        LIMIT 10
    """
    recent = manager.cursor.execute(recent_query).fetchall()

    # 3. Collection Stats
    stats = {}
    total_cards = manager.cursor.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
    unique_cards = manager.cursor.execute("SELECT COUNT(DISTINCT scryfall_id) FROM inventory").fetchone()[0]
    
    # Value Calculation (Foil aware)
    value_query = """
        SELECT SUM(
            CASE 
                WHEN i.finish = "foil" THEN COALESCE(cp.current_price_foil, 0)
                ELSE COALESCE(cp.current_price, 0)
            END
        )
        FROM inventory i
        JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
    """
    total_value = manager.cursor.execute(value_query).fetchone()[0] or 0.0

    # Ensure the key names match EXACTLY what you use in the HTML
    stats = {
        'total_cards': total_cards,
        'unique_cards': unique_cards,
        'total_value': total_value if total_value else 0.0
    }
        
    manager.close()
    
    return render_template('index.html', 
                           spotlight=spotlight, 
                           recent=recent, 
                           stats=stats)