"""
SQLite data layer for Johnny AI platform.
Single source of truth: listings table replaces the 24 hardcoded samples.
"""

import sqlite3
import json
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'listings.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS listings (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL DEFAULT 'agent_001',
            address TEXT NOT NULL,
            station TEXT DEFAULT '',
            walk_min INTEGER DEFAULT 0,
            price INTEGER NOT NULL,
            price_per_sqm REAL DEFAULT 0,
            size_sqm REAL DEFAULT 0,
            built_year INTEGER DEFAULT 0,
            age INTEGER DEFAULT 0,
            room_layout TEXT DEFAULT '',
            orientation TEXT DEFAULT '',
            floor INTEGER DEFAULT 0,
            total_floors INTEGER DEFAULT 0,
            structure TEXT DEFAULT '',
            land_rights TEXT DEFAULT '',
            type TEXT DEFAULT 'マンション',
            yield_surface REAL DEFAULT 0,
            yield_net REAL DEFAULT 0,
            source TEXT DEFAULT 'upload',
            photos TEXT DEFAULT '[]',
            floorplan_url TEXT DEFAULT '',
            reins_id TEXT DEFAULT '',
            ai_generated_copy TEXT DEFAULT '',
            ai_keywords TEXT DEFAULT '[]',
            disaster_flood TEXT DEFAULT 'low',
            disaster_earthquake TEXT DEFAULT 'low',
            disaster_liquefaction TEXT DEFAULT 'low',
            disaster_tsunami TEXT DEFAULT 'low',
            status TEXT DEFAULT 'draft' CHECK(status IN ('draft','published','archived')),
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            transit_lines TEXT DEFAULT '[]',
            ownership_type TEXT DEFAULT '',
            land_area_sqm REAL DEFAULT 0,
            land_area_tsubo REAL DEFAULT 0,
            land_category TEXT DEFAULT '',
            building_coverage_ratio INTEGER DEFAULT 0,
            floor_area_ratio INTEGER DEFAULT 0,
            city_planning_zone TEXT DEFAULT '',
            use_district TEXT DEFAULT '',
            roof_type TEXT DEFAULT '',
            floors_above INTEGER DEFAULT 0,
            built_date_full TEXT DEFAULT '',
            total_floor_area_sqm REAL DEFAULT 0,
            total_floor_area_tsubo REAL DEFAULT 0,
            floor_area_by_level TEXT DEFAULT '[]',
            current_status TEXT DEFAULT '',
            handover_timing TEXT DEFAULT '',
            transaction_type TEXT DEFAULT '',
            commission_type TEXT DEFAULT '',
            notes_freetext TEXT DEFAULT '',
            floorplan_images TEXT DEFAULT '[]',
            interior_photos TEXT DEFAULT '[]',
            listing_agent_name TEXT DEFAULT '',
            license_number TEXT DEFAULT '',
            brokerage_type TEXT DEFAULT '',
            latitude REAL DEFAULT 0,
            longitude REAL DEFAULT 0,
            market_reference_data TEXT DEFAULT '{}',
            mlit_checked_at TEXT DEFAULT '',
            mlit_use_district TEXT DEFAULT '',
            mlit_coverage_ratio INTEGER DEFAULT 0,
            mlit_floor_area_ratio INTEGER DEFAULT 0,
            mlit_disaster_flood TEXT DEFAULT '',
            mlit_disaster_high_tide TEXT DEFAULT '',
            mlit_disaster_tsunami TEXT DEFAULT '',
            mlit_disaster_landslide TEXT DEFAULT '',
            staged_photos TEXT DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            company TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);
        CREATE INDEX IF NOT EXISTS idx_listings_agent ON listings(agent_id);
        CREATE INDEX IF NOT EXISTS idx_listings_price ON listings(price);
        CREATE INDEX IF NOT EXISTS idx_listings_area ON listings(address);
        CREATE INDEX IF NOT EXISTS idx_listings_source ON listings(source);
    """)
    conn.commit()

    # ── Lightweight migrations for existing DBs ──
    try:
        existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(listings)").fetchall()}
        if 'staged_photos' not in existing_cols:
            conn.execute("ALTER TABLE listings ADD COLUMN staged_photos TEXT DEFAULT '[]'")
            conn.commit()
        for col, ddl in (
            ('reins_overview_pdf', "ALTER TABLE listings ADD COLUMN reins_overview_pdf TEXT DEFAULT ''"),
            ('reins_drawing_pdf', "ALTER TABLE listings ADD COLUMN reins_drawing_pdf TEXT DEFAULT ''"),
            ('reins_registered_at', "ALTER TABLE listings ADD COLUMN reins_registered_at TEXT DEFAULT ''"),
            ('reins_updated_at', "ALTER TABLE listings ADD COLUMN reins_updated_at TEXT DEFAULT ''"),
            ('building_name', "ALTER TABLE listings ADD COLUMN building_name TEXT DEFAULT ''"),
            ('management_company', "ALTER TABLE listings ADD COLUMN management_company TEXT DEFAULT ''"),
            ('management_type', "ALTER TABLE listings ADD COLUMN management_type TEXT DEFAULT ''"),
            ('registration_no', "ALTER TABLE listings ADD COLUMN registration_no TEXT DEFAULT ''"),
            ('underground_floors', "ALTER TABLE listings ADD COLUMN underground_floors INTEGER DEFAULT 0"),
        ):
            if col not in existing_cols:
                conn.execute(ddl)
                conn.commit()
    except Exception as e:
        print(f"[db migration] {e}", flush=True)

    # Seed disabled — start with empty DB
    conn.close()

def seed_samples(conn):
    """Seed 24 sample listings from existing REINS data, marked source='sample'."""
    samples = load_sample_data()
    for s in samples:
        conn.execute("""
            INSERT INTO listings (
                id, agent_id, address, station, walk_min, price, price_per_sqm,
                size_sqm, built_year, age, room_layout, orientation, floor, total_floors,
                structure, land_rights, type, yield_surface, yield_net,
                source, photos, floorplan_url, ai_generated_copy, ai_keywords,
                disaster_flood, disaster_earthquake, disaster_liquefaction, disaster_tsunami,
                status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            s['id'], s.get('agent_id','agent_001'), s['address'], s.get('station',''),
            s.get('walk_min',0), s['price'], s.get('price_per_sqm',0),
            s.get('size_sqm',0), s.get('built_year',0), s.get('age',0),
            s.get('room_layout',''), s.get('orientation',''),
            s.get('floor',0), s.get('total_floors',0),
            s.get('structure',''), s.get('land_rights',''),
            s.get('type','マンション'), s.get('yield_surface',0), s.get('yield_net',0),
            'sample', json.dumps(s.get('photos',[])), s.get('floorplan_url',''),
            s.get('ai_generated_copy',''), json.dumps(s.get('ai_keywords',[])),
            s.get('disaster_flood','low'), s.get('disaster_earthquake','low'),
            s.get('disaster_liquefaction','low'), s.get('disaster_tsunami','low'),
            'published'
        ))
    conn.commit()

def load_sample_data():
    """Load the 24 sample properties from existing JSON structure."""
    # These are the same 24 samples from the existing index.html DATA object
    return [
        {"id":"P20260001","address":"千代田区","price":9145,"price_per_sqm":152.3,"size_sqm":60,"age":12,"room_layout":"2LDK","floor":8,"total_floors":14,"station":"東京メトロ","walk_min":3,"orientation":"南","type":"マンション","structure":"RC","land_rights":"所有権","yield_surface":4.2,"yield_net":3.5,"disaster_flood":"low","disaster_earthquake":"low","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["千代田区","2LDK","駅近","投資用","RC造"],"ai_generated_copy":"千代田区の駅近2LDK。南向きで日当たり良好。築12年RC造。表面利回り4.2%。"},
        {"id":"P20260002","address":"中央区","price":8190,"price_per_sqm":144.9,"size_sqm":55,"age":8,"room_layout":"1LDK","floor":12,"total_floors":20,"station":"JR山手線","walk_min":5,"orientation":"南東","type":"マンション","structure":"SRC","land_rights":"所有権","yield_surface":5.1,"yield_net":4.2,"disaster_flood":"low","disaster_earthquake":"medium","disaster_liquefaction":"medium","disaster_tsunami":"low","ai_keywords":["中央区","1LDK","高層階","SRC造","投資用"],"ai_generated_copy":"中央区高層マンション。JR山手線徒歩5分。南東向き、築浅8年。表面利回り5.1%。"},
        {"id":"P20260003","address":"港区","price":13800,"price_per_sqm":183.8,"size_sqm":75,"age":5,"room_layout":"3LDK","floor":25,"total_floors":35,"station":"東京メトロ","walk_min":2,"orientation":"南西","type":"マンション","structure":"RC","land_rights":"所有権","yield_surface":3.8,"yield_net":3.1,"disaster_flood":"medium","disaster_earthquake":"low","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["港区","3LDK","タワーマンション","築浅","高級"],"ai_generated_copy":"港区タワーマンション25階。東京メトロ徒歩2分。南西向き3LDK、築5年。高級住宅地の一等地。"},
        {"id":"P20260004","address":"新宿区","price":5890,"price_per_sqm":110.3,"size_sqm":50,"age":18,"room_layout":"2K","floor":5,"total_floors":10,"station":"JR山手線","walk_min":7,"orientation":"西","type":"マンション","structure":"RC","land_rights":"所有権","yield_surface":5.5,"yield_net":4.3,"disaster_flood":"low","disaster_earthquake":"medium","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["新宿区","2K","JR山手線","高利回り","RC造"],"ai_generated_copy":"新宿区JR山手線徒歩7分。築18年RC造。表面利回り5.5%の高利回り物件。"},
        {"id":"P20260005","address":"渋谷区","price":9975,"price_per_sqm":162.8,"size_sqm":60,"age":10,"room_layout":"2LDK","floor":15,"total_floors":25,"station":"JR山手線","walk_min":4,"orientation":"南","type":"マンション","structure":"SRC","land_rights":"所有権","yield_surface":4.0,"yield_net":3.3,"disaster_flood":"low","disaster_earthquake":"low","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["渋谷区","2LDK","タワーマンション","南向き","SRC造"],"ai_generated_copy":"渋谷区タワーマンション15階。JR山手線徒歩4分。南向き2LDK、築10年。利便性抜群。"},
        {"id":"P20260006","address":"目黒区","price":7560,"price_per_sqm":123.9,"size_sqm":60,"age":14,"room_layout":"2LDK","floor":10,"total_floors":18,"station":"東急","walk_min":6,"orientation":"東","type":"マンション","structure":"RC","land_rights":"所有権","yield_surface":4.8,"yield_net":3.9,"disaster_flood":"low","disaster_earthquake":"medium","disaster_liquefaction":"medium","disaster_tsunami":"low","ai_keywords":["目黒区","2LDK","東急線","RC造","住環境"],"ai_generated_copy":"目黒区の落ち着いた住環境。東急線徒歩6分。2LDK、築14年。表面利回り4.8%。"},
        {"id":"P20260007","address":"世田谷区","price":6090,"price_per_sqm":96.6,"size_sqm":60,"age":20,"room_layout":"2LDK","floor":6,"total_floors":12,"station":"小田急","walk_min":8,"orientation":"南","type":"マンション","structure":"RC","land_rights":"所有権","yield_surface":5.8,"yield_net":4.6,"disaster_flood":"low","disaster_earthquake":"low","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["世田谷区","2LDK","高利回り","南向き","ファミリー"],"ai_generated_copy":"世田谷区ファミリー向け2LDK。南向き、小田急線徒歩8分。表面利回り5.8%の高利回り。"},
        {"id":"P20260008","address":"江東区","price":5460,"price_per_sqm":89.3,"size_sqm":60,"age":22,"room_layout":"3LDK","floor":8,"total_floors":15,"station":"都営地下鉄","walk_min":5,"orientation":"南東","type":"マンション","structure":"RC","land_rights":"所有権","yield_surface":5.2,"yield_net":4.0,"disaster_flood":"high","disaster_earthquake":"low","disaster_liquefaction":"medium","disaster_tsunami":"medium","ai_keywords":["江東区","3LDK","ファミリー","都営地下鉄","広々"],"ai_generated_copy":"江東区3LDKファミリーマンション。都営地下鉄徒歩5分。南東向き、60㎡の広々空間。"},
        {"id":"P20260009","address":"品川区","price":6825,"price_per_sqm":113.4,"size_sqm":60,"age":15,"room_layout":"2LDK","floor":12,"total_floors":22,"station":"JR山手線","walk_min":3,"orientation":"南","type":"マンション","structure":"SRC","land_rights":"所有権","yield_surface":4.5,"yield_net":3.7,"disaster_flood":"low","disaster_earthquake":"low","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["品川区","2LDK","駅近","SRC造","高層階"],"ai_generated_copy":"品川区駅近マンション。JR山手線徒歩3分。南向き2LDK、12階SRC造。通勤至便。"},
        {"id":"P20260010","address":"横浜市","price":3675,"price_per_sqm":65.1,"size_sqm":55,"age":25,"room_layout":"2LDK","floor":5,"total_floors":10,"station":"JR山手線","walk_min":10,"orientation":"南","type":"マンション","structure":"RC","land_rights":"所有権","yield_surface":6.2,"yield_net":4.9,"disaster_flood":"low","disaster_earthquake":"medium","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["横浜市","2LDK","高利回り","RC造","投資用"],"ai_generated_copy":"横浜市高利回り物件。表面利回り6.2%。2LDK、南向き。投資用として優良。"},
        {"id":"P20260011","address":"文京区","price":7140,"price_per_sqm":115.5,"size_sqm":60,"age":11,"room_layout":"2LDK","floor":9,"total_floors":16,"station":"東京メトロ","walk_min":4,"orientation":"南東","type":"マンション","structure":"RC","land_rights":"所有権","yield_surface":4.3,"yield_net":3.5,"disaster_flood":"low","disaster_earthquake":"low","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["文京区","2LDK","文教地区","東京メトロ","RC造"],"ai_generated_copy":"文京区文教地区の2LDK。東京メトロ徒歩4分。南東向き、築11年。落ち着いた住環境。"},
        {"id":"P20260012","address":"台東区","price":5775,"price_per_sqm":99.8,"size_sqm":55,"age":19,"room_layout":"1LDK","floor":7,"total_floors":12,"station":"JR山手線","walk_min":6,"orientation":"西","type":"マンション","structure":"RC","land_rights":"所有権","yield_surface":5.3,"yield_net":4.1,"disaster_flood":"low","disaster_earthquake":"medium","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["台東区","1LDK","JR山手線","単身向け","RC造"],"ai_generated_copy":"台東区JR山手線徒歩6分。1LDK単身向けマンション。表面利回り5.3%。"},
        {"id":"P20260013","address":"墨田区","price":4410,"price_per_sqm":78.8,"size_sqm":55,"age":24,"room_layout":"2K","floor":4,"total_floors":10,"station":"都営地下鉄","walk_min":8,"orientation":"東","type":"マンション","structure":"RC","land_rights":"所有権","yield_surface":5.9,"yield_net":4.6,"disaster_flood":"medium","disaster_earthquake":"medium","disaster_liquefaction":"medium","disaster_tsunami":"low","ai_keywords":["墨田区","2K","高利回り","都営地下鉄","投資用"],"ai_generated_copy":"墨田区の高利回り2K。表面利回り5.9%。都営地下鉄徒歩8分。投資用物件。"},
        {"id":"P20260014","address":"大田区","price":4725,"price_per_sqm":81.9,"size_sqm":55,"age":21,"room_layout":"2LDK","floor":6,"total_floors":11,"station":"京王","walk_min":9,"orientation":"南","type":"マンション","structure":"RC","land_rights":"所有権","yield_surface":5.6,"yield_net":4.4,"disaster_flood":"low","disaster_earthquake":"low","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["大田区","2LDK","南向き","ファミリー","RC造"],"ai_generated_copy":"大田区ファミリー向け2LDK。南向き、京王線徒歩9分。表面利回り5.6%。"},
        {"id":"P20260015","address":"中野区","price":5040,"price_per_sqm":86.1,"size_sqm":55,"age":16,"room_layout":"2LDK","floor":5,"total_floors":10,"station":"西武","walk_min":7,"orientation":"南東","type":"マンション","structure":"RC","land_rights":"所有権","yield_surface":5.4,"yield_net":4.2,"disaster_flood":"low","disaster_earthquake":"low","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["中野区","2LDK","西武線","RC造","南東向き"],"ai_generated_copy":"中野区の2LDK。西武線徒歩7分。南東向き、築16年。表面利回り5.4%。"},
        {"id":"P20260016","address":"杉並区","price":4830,"price_per_sqm":84.0,"size_sqm":55,"age":17,"room_layout":"2LDK","floor":4,"total_floors":8,"station":"JR山手線","walk_min":10,"orientation":"南","type":"マンション","structure":"RC","land_rights":"所有権","yield_surface":5.7,"yield_net":4.5,"disaster_flood":"low","disaster_earthquake":"low","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["杉並区","2LDK","南向き","JR線","高利回り"],"ai_generated_copy":"杉並区の南向き2LDK。JR線徒歩10分。表面利回り5.7%。静かな住宅街。"},
        {"id":"P20260017","address":"豊島区","price":5355,"price_per_sqm":92.4,"size_sqm":55,"age":15,"room_layout":"1LDK","floor":10,"total_floors":18,"station":"JR山手線","walk_min":3,"orientation":"南","type":"マンション","structure":"SRC","land_rights":"所有権","yield_surface":4.9,"yield_net":3.9,"disaster_flood":"low","disaster_earthquake":"medium","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["豊島区","1LDK","駅近","SRC造","高層階"],"ai_generated_copy":"豊島区駅近1LDK。JR山手線徒歩3分。南向き10階、SRC造。池袋エリア好立地。"},
        {"id":"P20260018","address":"北区","price":3990,"price_per_sqm":71.4,"size_sqm":55,"age":28,"room_layout":"2DK","floor":3,"total_floors":8,"station":"JR山手線","walk_min":11,"orientation":"西","type":"マンション","structure":"RC","land_rights":"所有権","yield_surface":6.5,"yield_net":5.1,"disaster_flood":"low","disaster_earthquake":"low","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["北区","2DK","高利回り","投資用","RC造"],"ai_generated_copy":"北区の高利回り2DK。表面利回り6.5%、実質5.1%。投資用として最適。"},
        {"id":"P20260019","address":"川崎市","price":3990,"price_per_sqm":71.4,"size_sqm":55,"age":23,"room_layout":"2LDK","floor":5,"total_floors":10,"station":"JR山手線","walk_min":8,"orientation":"南","type":"マンション","structure":"RC","land_rights":"所有権","yield_surface":6.0,"yield_net":4.7,"disaster_flood":"low","disaster_earthquake":"low","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["川崎市","2LDK","高利回り","JR線","ファミリー"],"ai_generated_copy":"川崎市のファミリー向け2LDK。JR線徒歩8分。表面利回り6.0%。南向き日当たり良好。"},
        {"id":"P20260020","address":"足立区","price":3360,"price_per_sqm":63.0,"size_sqm":50,"age":30,"room_layout":"2DK","floor":3,"total_floors":7,"station":"東武","walk_min":12,"orientation":"東","type":"マンション","structure":"RC","land_rights":"所有権","yield_surface":6.8,"yield_net":5.3,"disaster_flood":"medium","disaster_earthquake":"low","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["足立区","2DK","高利回り","東武線","投資用"],"ai_generated_copy":"足立区の高利回り2DK。表面利回り6.8%、実質5.3%。東武線徒歩12分。投資用物件。"},
        {"id":"P20260021","address":"千代田区","price":10235,"price_per_sqm":145.0,"size_sqm":70,"age":7,"room_layout":"3LDK","floor":20,"total_floors":30,"station":"東京メトロ","walk_min":2,"orientation":"南","type":"マンション","structure":"SRC","land_rights":"所有権","yield_surface":3.5,"yield_net":2.8,"disaster_flood":"low","disaster_earthquake":"low","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["千代田区","3LDK","タワーマンション","駅近","高級"],"ai_generated_copy":"千代田区タワーマンション20階。東京メトロ徒歩2分。南向き3LDK、70㎡。ラグジュアリー住戸。"},
        {"id":"P20260022","address":"渋谷区","price":8550,"price_per_sqm":155.0,"size_sqm":55,"age":12,"room_layout":"1LDK","floor":18,"total_floors":28,"station":"東京メトロ","walk_min":1,"orientation":"南東","type":"マンション","structure":"SRC","land_rights":"所有権","yield_surface":4.0,"yield_net":3.2,"disaster_flood":"low","disaster_earthquake":"low","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["渋谷区","1LDK","駅直結","SRC造","高層階"],"ai_generated_copy":"渋谷区駅直結1LDK。東京メトロ徒歩1分。18階SRC造、南東向き。渋谷の中心地。"},
        {"id":"P20260023","address":"港区","price":10800,"price_per_sqm":175.0,"size_sqm":60,"age":3,"room_layout":"2LDK","floor":30,"total_floors":40,"station":"JR山手線","walk_min":1,"orientation":"南","type":"マンション","structure":"RC","land_rights":"所有権","yield_surface":3.2,"yield_net":2.5,"disaster_flood":"medium","disaster_earthquake":"low","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["港区","2LDK","プレミアム","タワーマンション","駅直結"],"ai_generated_copy":"港区プレミアムタワー30階。JR山手線徒歩1分。築3年、南向き2LDK。最高級レジデンス。"},
        {"id":"P20260024","address":"武蔵野市","price":5775,"price_per_sqm":94.5,"size_sqm":60,"age":13,"room_layout":"2LDK","floor":6,"total_floors":12,"station":"JR山手線","walk_min":5,"orientation":"南","type":"マンション","structure":"RC","land_rights":"所有権","yield_surface":4.6,"yield_net":3.7,"disaster_flood":"low","disaster_earthquake":"low","disaster_liquefaction":"low","disaster_tsunami":"low","ai_keywords":["武蔵野市","2LDK","JR線","吉祥寺","住環境"],"ai_generated_copy":"武蔵野市吉祥寺エリア。JR線徒歩5分。南向き2LDK、築13年。良好な住環境。"},
    ]