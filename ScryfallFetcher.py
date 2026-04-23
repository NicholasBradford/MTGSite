import requests, time, shutil, os
from datetime import timedelta,datetime

# Global headers for Scryfall API compliance
headers = {'User-Agent': 'Mozilla/5.0 (MTG-Collection-Tracker/1.0)'}
IMAGE_PATH = os.environ.get('IMAGE_PATH')
class ScryfallFetcher:
    def __init__(self, db_manager):
        self.db = db_manager
        self.base_url = "https://api.scryfall.com/cards"
        self.image_dir = f"{IMAGE_PATH}/img/cards"
        self.icon_dir = f"{IMAGE_PATH}/img/icons"
        
        # Ensure directories exist
        for directory in [self.image_dir, self.icon_dir]:
            if not os.path.exists(directory):
                os.makedirs(directory)
    
    def ensure_set_is_fully_populated(self, set_code):
        """Checks if a set is in the DB; if not, pulls every card printing for that set."""
        set_code = set_code.lower()
        
        # 1. Check if we have already processed this set (either downloaded or skipped)
        self.db.cursor.execute("SELECT set_code FROM sets WHERE set_code = ?", (set_code,))
        if self.db.cursor.fetchone():
            return 

        # 2. Fetch Set Metadata
        set_res = requests.get(f"https://api.scryfall.com/sets/{set_code}", headers=headers)
        if set_res.status_code != 200:
            return
            
        set_data = set_res.json()
        
        # 3. Determine eligibility
        set_type = set_data.get('set_type')
        is_expansion = set_type == 'expansion'
        release_date_str = set_data.get('released_at')
        is_recent = False
        
        if release_date_str:
            release_date = datetime.strptime(release_date_str, '%Y-%m-%d')
            three_years_ago = datetime.now() - timedelta(days=1095)
            is_recent = release_date > three_years_ago
        
        is_standard_legal = 1 if (is_expansion and is_recent) else 0
        # 4. Handle Icon Download (Common for both skipped and synced sets)
        icon_url = set_data.get('icon_svg_uri')
        local_icon_path = f"img/icons/{set_code}.svg"
        full_fs_path = os.path.join(IMAGE_PATH, local_icon_path)
        
        if icon_url and not os.path.exists(full_fs_path):
            os.makedirs(os.path.dirname(full_fs_path), exist_ok=True)
            img_res = requests.get(icon_url, headers=headers)
            if img_res.status_code == 200:
                with open(full_fs_path, 'wb') as f:
                    f.write(img_res.content)

        # 5. Insert set into 'sets' table immediately 
        # This ensures we don't re-query Scryfall for metadata on skipped sets
        self.db.cursor.execute("""
            INSERT OR REPLACE INTO sets (set_code, set_name, set_type, standard_legal, released_at, icon_svg_uri) 
            VALUES (?, ?, ?, ?, ?, ?)
        """, (set_code, set_data['name'], set_type, is_standard_legal, set_data.get('released_at'), local_icon_path))
        self.db.commit()

        # 6. Exit early if it doesn't meet your "Bulk Download" criteria
        if not (is_expansion and is_recent):
            print(f"Skipping bulk card download for {set_code.upper()} (Not a recent expansion).")
            return

        print(f"Standard Expansion confirmed: {set_code.upper()}. Syncing all cards...")
        
        # 7. Fetch EVERY card printing (excluding basic lands)
        search_url = f"https://api.scryfall.com/cards/search?q=set:{set_code}+-type:basic&unique=prints"
        
        while search_url:
            cards_res = requests.get(search_url, headers=headers)
            if cards_res.status_code != 200:
                break
                
            cards_data = cards_res.json()
            for card in cards_data.get('data', []):
                scryfall_id = card.get('id')
                oracle_id = card.get('oracle_id')
                if 'image_uris' in card:
                    image_url = card.get('image_uris', {}).get('normal', '')
                elif 'card_faces' in card:
                    # Gets the image of the front face (Peter Parker)
                    image_url = card['card_faces'][0]['image_uris']['normal']
                # Setup Paths
                local_img_path = f"img/cards/{set_code}/{scryfall_id}.jpg"
                full_fs_path = os.path.join(IMAGE_PATH, local_img_path)

                # 1. Download Image locally if it doesn't exist
                if image_url and not os.path.exists(full_fs_path):
                    os.makedirs(os.path.dirname(full_fs_path), exist_ok=True)
                    try:
                        with requests.get(image_url, stream=True, headers=headers, timeout=10) as img_res:
                            if img_res.status_code == 200:
                                with open(full_fs_path, 'wb') as f:
                                    shutil.copyfileobj(img_res.raw, f)
                                time.sleep(0.1) # Be a good citizen
                    except Exception as e:
                        print(f"Error downloading {card.get('name')}: {e}")

                # 2. A. Populate card_definitions
                self.db.cursor.execute("""
                    INSERT OR IGNORE INTO card_definitions 
                    (oracle_id, name, mana_cost, cmc, type_line, oracle_text, color_identity)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    card.get('oracle_id'), card.get('name'), card.get('mana_cost'),
                    card.get('cmc'), card.get('type_line'), card.get('oracle_text'),
                    "".join(card.get('color_identity', []))
                ))

                # 3. B. Populate card_printings with the LOCAL path
                self.db.cursor.execute("""
                    INSERT OR IGNORE INTO card_printings 
                    (scryfall_id, oracle_id, set_code, collector_number, rarity, image_url)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    scryfall_id, oracle_id, set_code,
                    card.get('collector_number'), card.get('rarity'), local_img_path
                ))

            search_url = cards_data.get('next_page')
            if search_url:
                time.sleep(0.1)

        self.db.commit()
        print(f"Set {set_code.upper()} fully synchronized.")

    def fetch_and_add(self, set_code, collector_number):     
        set_code = set_code.lower()
        # Trigger the full set sync first to ensure checklist is ready
        self.ensure_set_is_fully_populated(set_code)

        # 1. Request the specific card from Scryfall
        url = f"{self.base_url}/{set_code}/{collector_number}"
        response = requests.get(url, headers=headers)
        
        if response.status_code != 200:
            print(f"Error: Could not find {set_code} {collector_number}")
            return False

        data = response.json()
        scryfall_id = data.get('id')
        oracle_id = data.get('oracle_id')
        
        # 2. Local Image Download (Specific to the version being added)
        img_url = data.get('image_uris', {}).get('normal')
        local_img_path = f"img/cards/{set_code}/{scryfall_id}.jpg"
        full_img_fs_path = os.path.join(IMAGE_PATH, local_img_path)
        
        if img_url and not os.path.exists(full_img_fs_path):
            os.makedirs(os.path.dirname(full_img_fs_path), exist_ok=True)
            img_res = requests.get(img_url, stream=True)
            if img_res.status_code == 200:
                with open(full_img_fs_path, 'wb') as f:
                    shutil.copyfileobj(img_res.raw, f)
         
        prices = data.get('prices', {})        
        non_foil_price = prices.get("usd")
        foil_price = prices.get("usd_foil")

        # 3. DB Transaction
        try:
            # Update definitions just in case
            self.db.cursor.execute('''
                INSERT OR IGNORE INTO card_definitions (oracle_id, name, mana_cost, cmc, type_line, oracle_text, color_identity)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                oracle_id, data.get('name'), data.get('mana_cost'), 
                data.get('cmc'), data.get('type_line'), data.get('oracle_text'),
                "".join(data.get('color_identity', []))
            ))
            
            # Ensure price history exists
            self.db.cursor.execute('''
                INSERT INTO price_history (scryfall_id, price_usd, price_foil)
                VALUES (?, ?, ?)
            ''', (scryfall_id, non_foil_price, foil_price))

            # Ensure this printing is marked as the "local image" version
            self.db.cursor.execute('''
                INSERT OR REPLACE INTO card_printings (scryfall_id, oracle_id, set_code, collector_number, rarity, image_url, flavor_text, current_price, current_price_foil)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                scryfall_id, oracle_id, set_code, 
                collector_number, data.get('rarity'), local_img_path, data.get('flavor_text'),
                non_foil_price, foil_price
            ))

            self.db.commit()
            time.sleep(0.1)
            return scryfall_id

        except Exception as e:
            print(f"DB Error: {e}")
            return False