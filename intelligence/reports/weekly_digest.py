# intelligence/reports/weekly_digest.py
# Sends automated weekly performance emails
# Target: music managers who subscribe to pro tier

import resend
from datetime import date, timedelta
from database.client import get_supabase

def generate_weekly_digest(artist_ids: list[str]) -> str:
    """Generate HTML email body for a manager's roster."""
    db = get_supabase()
    sections = []
    
    for artist_id in artist_ids:
        artist = db.table("artists").select("*").eq(
            "id", artist_id
        ).single().execute().data
        
        health = db.table("artist_health_scores").select("*").eq(
            "artist_id", artist_id
        ).order("score_date", desc=True).limit(2).execute().data
        
        current_score = health[0]["score"] if health else None
        prev_score = health[1]["score"] if len(health) > 1 else None
        
        score_change = ""
        if current_score and prev_score:
            delta = current_score - prev_score
            score_change = f"({'↑' if delta > 0 else '↓'}{abs(delta):.1f})"
        
        # New milestones this week
        milestones = db.table("artist_milestones").select(
            "milestone_text"
        ).eq("artist_id", artist_id).gte(
            "achieved_at",
            str(date.today() - timedelta(days=7))
        ).execute().data
        
        sections.append(f"""
        <div style="border:1px solid #e2e8f0;border-radius:8px;padding:20px;margin-bottom:16px;">
            <h3 style="margin:0 0 8px;">{artist['name']}</h3>
            <p>Health Score: <strong>{current_score:.0f}/100</strong> {score_change}</p>
            {"<p>🏆 " + " | ".join(m['milestone_text'] for m in milestones) + "</p>" if milestones else ""}
        </div>
        """)
    
    return "\n".join(sections)