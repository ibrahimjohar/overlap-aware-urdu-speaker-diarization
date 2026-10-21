"""peek.py - quick look at the provenance database during the pilot (read-only).

    python peek.py funnel            # how many items reached each stage
    python peek.py reasons           # metadata screening outcomes, counted by reason
    python peek.py rejects [N]       # N random rejected videos with reason, minutes, title
    python peek.py passes [N]        # N random videos that passed metadata screening
    python peek.py channels [N]      # channels with the most passing videos (for vetting)
    python peek.py screening [N]     # latest audio screening results, best score first
    python peek.py profiles [N]      # seed-channel licence profiles (from s00_seed_channels.py)

Avoids PowerShell quoting problems with `python -c "..."`. Never changes the database.
"""
import sys

from common import db

QUERIES = {
    "funnel": ("""
        SELECT 'discovered', COUNT(*) FROM videos
        UNION ALL SELECT 'metadata pass', COUNT(*) FROM videos WHERE meta_status='pass'
        UNION ALL SELECT 'channels approved', COUNT(*) FROM channels WHERE vetting='approved'
        UNION ALL SELECT 'downloaded', COUNT(*) FROM downloads WHERE status='ok'
        UNION ALL SELECT 'probe screened', COUNT(*) FROM screening WHERE stage='probe'
        UNION ALL SELECT 'full screened', COUNT(*) FROM screening WHERE stage='full'
        UNION ALL SELECT 'accepted in triage', COUNT(DISTINCT video_id) FROM triage WHERE decision='accept'
        UNION ALL SELECT 'excerpts', COUNT(*) FROM excerpts""", False),
    "reasons": ("""
        SELECT meta_status, COALESCE(NULLIF(meta_reason, ''), '-') AS reason, tier_prelim, COUNT(*)
        FROM videos GROUP BY 1, 2, 3 ORDER BY 4 DESC""", False),
    "rejects": ("""
        SELECT meta_reason, ROUND(duration_s / 60.0, 1), tier_prelim, title FROM videos
        WHERE meta_status='reject' ORDER BY RANDOM() LIMIT ?""", True),
    "passes": ("""
        SELECT ROUND(duration_s / 60.0, 1), tier_prelim, video_id, title FROM videos
        WHERE meta_status='pass' ORDER BY RANDOM() LIMIT ?""", True),
    "channels": ("""
        SELECT c.channel_id, c.vetting, COUNT(*) AS passing,
               SUM(v.tier_prelim LIKE 'A%') AS cc, c.title
        FROM videos v JOIN channels c ON c.channel_id = v.channel_id
        WHERE v.meta_status='pass' GROUP BY c.channel_id ORDER BY passing DESC LIMIT ?""", True),
    "screening": ("""
        SELECT video_id, stage, score, n_speakers, ROUND(overlap_ratio * 100, 1),
               ROUND(urdu_hindi_frac * 100), ROUND(music_ratio * 100)
        FROM screening ORDER BY score DESC LIMIT ?""", True),
    "profiles": ("""
        SELECT p.category, CAST(ROUND(p.cc_share * 100) AS INT), CAST(ROUND(p.claimed_share * 100) AS INT), CAST(ROUND(p.long_share * 100) AS INT),
               c.vetting, c.title, p.suggestion
        FROM channel_profile p JOIN channels c USING(channel_id)
        WHERE p.profiled_at = (SELECT MAX(profiled_at) FROM channel_profile p2 WHERE p2.channel_id = p.channel_id)
        ORDER BY p.known_licence != 'pd', p.cc_share - p.claimed_share DESC LIMIT ?""", True),
}
HEADERS = {
    "funnel": ["stage", "count"],
    "reasons": ["status", "reason", "tier", "count"],
    "rejects": ["reason", "min", "tier", "title"],
    "passes": ["min", "tier", "video_id", "title"],
    "channels": ["channel_id", "vetting", "passing", "cc", "title"],
    "screening": ["video_id", "stage", "score", "spk", "ovl%", "urdu%", "music%"],
    "profiles": ["category", "cc%", "claimed%", "long%", "vetting", "channel", "suggestion"],
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in QUERIES:
        print(__doc__)
        return
    name = sys.argv[1]
    sql, takes_n = QUERIES[name]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    rows = db().execute(sql, (n,) if takes_n else ()).fetchall()
    print(" | ".join(HEADERS[name]))
    for r in rows:
        print(" | ".join("" if x is None else str(x)[:70] for x in r))


if __name__ == "__main__":
    main()