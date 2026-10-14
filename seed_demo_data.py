import os
import psycopg
from app.security import hash_password

URI = os.getenv("DATABASE_URL", "postgresql://ekyama:ekyama_password@localhost:5432/ekyama_test")

def seed():
    print(f"[*] Connecting to database: {URI}")
    with psycopg.connect(URI, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Ensure schema tables exist
            schema_file = os.path.join(os.path.dirname(__file__), "schema.sql")
            if os.path.exists(schema_file):
                cur.execute("select exists (select from information_schema.tables where table_name = 'profiles')")
                if not cur.fetchone()[0]:
                    print("[*] Creating schema tables from schema.sql...")
                    cur.execute(open(schema_file, encoding="utf-8").read())

            print("[*] Clearing existing tables...")
            cur.execute("truncate sentence_skips, sentences, payouts, payment_methods, refresh_tokens, password_reset_tokens, profiles restart identity cascade")

            # Configure reward settings: 10,000,000 UGX budget cap, 2000 rate, 5000 min cashout
            cur.execute("""
                insert into reward_settings (id, rate_ugx, min_reward_ugx, min_cashout_ugx, max_budget_ugx, updated_at)
                values (true, 2000, 100, 5000, 10000000, now())
                on conflict (id) do update set
                    rate_ugx = 2000, min_reward_ugx = 100, min_cashout_ugx = 5000, max_budget_ugx = 10000000, updated_at = now()
            """)

            pwd_hash = hash_password("Password123!")

            # 1. Seed profiles
            print("[*] Seeding profiles with password 'Password123!'...")
            cur.execute("""
                insert into profiles (email, password_hash, full_name, role, is_active)
                values
                    ('admin@ekyama.health', %s, 'Dr. Kizito Mayanja (Admin)', 'admin', true),
                    ('reviewer@ekyama.health', %s, 'Dr. Sarah Namubiru (Reviewer)', 'reviewer', true),
                    ('validator@ekyama.health', %s, 'Amina Nabukeera (Validator)', 'validator', true),
                    ('paul.kato@ekyama.health', %s, 'Paul Kato (Validator)', 'validator', true),
                    ('grace.alupo@ekyama.health', %s, 'Grace Alupo (Validator)', 'validator', true)
                returning id, email, role
            """, (pwd_hash, pwd_hash, pwd_hash, pwd_hash, pwd_hash))

            profiles = {row[1]: row[0] for row in cur.fetchall()}
            admin_id = profiles['admin@ekyama.health']
            reviewer_id = profiles['reviewer@ekyama.health']
            validator_id = profiles['validator@ekyama.health']
            paul_id = profiles['paul.kato@ekyama.health']
            grace_id = profiles['grace.alupo@ekyama.health']

            # 2. Seed payment methods
            print("[*] Seeding payment methods...")
            cur.execute("""
                insert into payment_methods (user_id, provider, phone_number, account_name, is_default)
                values
                    (%s, 'mtn', '+256782334455', 'Amina Nabukeera', true),
                    (%s, 'airtel', '+256755443322', 'Paul Kato', true),
                    (%s, 'mtn', '+256772110099', 'Grace Alupo', true)
            """, (validator_id, paul_id, grace_id))

            # 3. Seed Payouts
            print("[*] Seeding payouts (pending, paid, rejected)...")
            # Pending payout 1: Amina
            cur.execute("""
                insert into payouts (user_id, amount_ugx, provider, phone_number, account_name, status, requested_at)
                values (%s, 45000, 'mtn', '+256782334455', 'Amina Nabukeera', 'pending', now() - interval '2 hours')
                returning id
            """, (validator_id,))
            payout_pending_1 = cur.fetchone()[0]

            # Pending payout 2: Grace
            cur.execute("""
                insert into payouts (user_id, amount_ugx, provider, phone_number, account_name, status, requested_at)
                values (%s, 68000, 'airtel', '+256701889900', 'Grace Alupo', 'pending', now() - interval '45 minutes')
                returning id
            """, (grace_id,))
            payout_pending_2 = cur.fetchone()[0]

            # Paid payout 3: Paul
            cur.execute("""
                insert into payouts (user_id, amount_ugx, provider, phone_number, account_name, status, requested_at, processed_by, processed_at, provider_reference)
                values (%s, 75000, 'airtel', '+256755443322', 'Paul Kato', 'paid', now() - interval '1 day', %s, now() - interval '18 hours', 'AIRTEL-908123-UG')
                returning id
            """, (paul_id, admin_id))
            payout_paid_3 = cur.fetchone()[0]

            # Rejected payout 4: Amina (previous test)
            cur.execute("""
                insert into payouts (user_id, amount_ugx, provider, phone_number, account_name, status, requested_at, processed_by, processed_at, rejection_reason)
                values (%s, 12000, 'mtn', '+256782334455', 'Amina Nabukeera', 'rejected', now() - interval '3 days', %s, now() - interval '2 days', 'Account name mismatch with registered MTN SIM')
                returning id
            """, (validator_id, admin_id))

            # 4. Seed Sentences
            print("[*] Seeding clinical sentences across all workflow stages...")
            
            # Group A: Sentences awaiting clinical review in /review
            review_sentences = [
                (
                    1, "Do you experience sharp, stabbing pain in your chest when inhaling deeply?",
                    "Ono wulira obulumi obusala mu kifuba ng'ossaamu omukka omungi?",
                    "Owulira obulumi obusala ng'obw'ekiso mu kifuba ng'ossaamu omukka ogw'amaanyi?",
                    0.62, "hard", validator_id, 1240
                ),
                (
                    2, "Have you noticed any dark blood clots in your stool or bowel movements over the past three days?",
                    "Olabyeko omusaayi omukwafu mu bubi bwo mu nnaku essatu eziyise?",
                    "Olabyeko ebitole by'omusaayi omukwafu omuddugavu mu bulyo bwo mu nnaku essatu eziyise?",
                    0.68, "hard", paul_id, 1350
                ),
                (
                    3, "Are you taking any prescribed medication for high blood pressure or diabetes?",
                    "Oli ku ddagala lyonna erya puleesa oba sukaali?",
                    "Oli ku ddagala lyonna eryawandiikibwa omusawo erya puleesa oba sukaali?",
                    0.46, "medium", grace_id, 920
                ),
                (
                    4, "Has the child been having convulsive seizures or vomiting everything they drink?",
                    "Omwana alina okusimbibwa oba okusesema byonna by'anywa?",
                    "Omwana abaddemu ensimbu oba okusesema buli ky'anywa?",
                    0.80, "hard", validator_id, 1600
                ),
                (
                    5, "Does the patient have a stiff neck, sensitivity to light, or altered mental status?",
                    "Omulwadde alina ensingo engumu oba okutya ekitangaala?",
                    "Omulwadde alina ensingo engumu, okulumizibwa ekitangaala, oba okutabulwa mu bwongo?",
                    0.75, "hard", paul_id, 1500
                ),
            ]

            for s in review_sentences:
                cur.execute("""
                    insert into sentences (
                        source_row, noisy_english, attempted, clean_lug, length_words,
                        cost_raw, cost, difficulty_score, difficulty,
                        validated_by, validated_at, reward_ugx, verified
                    ) values (
                        %s, %s, %s, %s, 10,
                        %s, %s, %s, %s,
                        %s, now() - interval '3 hours', %s, false
                    )
                """, (s[0], s[1], s[2], s[3], s[4], s[4], s[4], s[5], s[6], s[7]))

            # Group B: Sentences tied to pending & paid payouts
            cur.execute("""
                insert into sentences (
                    source_row, noisy_english, attempted, clean_lug, length_words,
                    cost_raw, cost, difficulty_score, difficulty,
                    validated_by, validated_at, reward_ugx, payout_id, paid_status, paid_at, verified, verified_by, verified_at
                ) values
                    (10, 'Any persistent cough with bloody sputum for more than two weeks?', 'Ekifuba kyonna ekirimu omusaayi okumala wiiki bbiri?', 'Olina ekifuba ekirese n''akakololo akalimu omusaayi okumala wiiki ezisukka mu bbiri?', 12, 0.75, 0.75, 0.75, 'hard', %s, now() - interval '2 days', 1500, %s, true, now() - interval '18 hours', true, %s, now() - interval '20 hours'),
                    (11, 'Has your fever subsided after completing the course of antimalarials?', 'Omusujja gukendedde oluvannyuma lw''eddagala ly''omusujja?', 'Omusujja gwakkakkana oluvannyuma lw''okumalayo eddagala ly''omusujja gw''ensiri?', 10, 0.50, 0.50, 0.50, 'medium', %s, now() - interval '3 hours', 1000, %s, false, null, true, %s, now() - interval '1 hour')
            """, (paul_id, payout_paid_3, admin_id, validator_id, payout_pending_1, reviewer_id))

            # Group C: Unvalidated sentences ready for translators in /work
            unvalidated = [
                (20, "How many weeks pregnant are you, and have you experienced any vaginal bleeding?", "Olina embuto ya wiiki mmeka, era olabyeko omusaayi?", 0.65, "hard"),
                (21, "Does the rash itch intensely during the night or spread after bathing with warm water?", "Olubuto lulumya nnyo ekiro oba lusaasaana ng'onaabye amazzi ag'ebbugumu?", 0.55, "medium"),
                (22, "When was your last dose of anti-retroviral treatment or tuberculosis medication?", "Ddi lwe wasemba okumira eddagala ly'akawuka oba ery'akafuba?", 0.50, "medium"),
                (23, "Is the severe headache accompanied by neck stiffness, photophobia, or high fever?", "Omutwe oguluma ennyo guwerekerwa ensingo engumu oba omusujja omungi?", 0.70, "hard"),
                (24, "Have you been diagnosed with sickle cell anemia in your immediate family?", "Waliwo eyakeberebwako nalangibwa nalubiri mu maka go?", 0.40, "medium"),
                (25, "Are you experiencing difficulty swallowing solid foods or liquids?", "Osobodde okumira emmere enkalu oba ebyokunywa?", 0.35, "medium"),
                (26, "Have you noticed any swelling in both feet or breathlessness when lying flat?", "Olinako okuzimba amagulu gombi oba okuziyira ng'ogalamidde obugazi?", 0.60, "hard"),
                (27, "Do you smoke cigarettes or drink alcohol regularly?", "Onywa taaba oba omwenge buli lwe wabeerawo?", 0.05, "easy"),
            ]

            for u in unvalidated:
                cur.execute("""
                    insert into sentences (
                        source_row, noisy_english, attempted, length_words,
                        cost_raw, cost, difficulty_score, difficulty
                    ) values (
                        %s, %s, %s, 8,
                        %s, %s, %s, %s
                    )
                """, (u[0], u[1], u[2], u[3], u[3], u[3], u[4]))

            print("[+] Successfully seeded database!")
            print("[+] Ready for local end-to-end testing.")

if __name__ == "__main__":
    seed()
