"""Seed script for local development and demonstration."""
import sys
import os

# Ensure backend root is on sys.path
sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.session import SessionLocal
from app.scripts_runner import run_demo_seeder


def seed_database() -> None:
    """Populate local development database with rich demonstration dataset."""
    db = SessionLocal()
    try:
        print("[SEED] Starting local database demo data seeding...")
        summary = run_demo_seeder(db)
        print("[SEED] Demo data seeding completed successfully!")
        for k, v in summary.items():
            print(f"  - {k}: {str(v).replace('₹', 'INR ')}")
    except Exception as e:
        print(f"[SEED ERROR] Seeding failed: {e}")
        db.rollback()
        raise e
    finally:
        db.close()


if __name__ == "__main__":
    seed_database()
