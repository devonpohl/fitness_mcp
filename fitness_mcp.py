#!/usr/bin/env python3
"""
Fitness Tracker MCP Server

A local MCP server for tracking workouts, nutrition, body metrics, and training programs.
Designed to help coach users toward their fitness goals with data-driven insights.
"""

import json
import sqlite3
import os
from datetime import datetime, timedelta, date
from typing import Optional, List, Dict, Any
from enum import Enum
from pathlib import Path
from contextlib import contextmanager
import csv
import re

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, field_validator, ConfigDict

# Google Calendar imports (optional - graceful fallback if not installed)
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    GOOGLE_CALENDAR_AVAILABLE = True
except ImportError:
    GOOGLE_CALENDAR_AVAILABLE = False

# Initialize the MCP server
mcp = FastMCP("fitness_mcp")

# Database path — use DB_PATH env var if set (for remote deploy with
# persistent volume at /data), otherwise default to local home directory.
DB_PATH = os.environ.get("DB_PATH", os.path.expanduser("~/.fitness_tracker/fitness.db"))


# ============================================================================
# Database Setup
# ============================================================================

def get_db_path() -> str:
    """Get the database path, creating directory if needed."""
    db_dir = os.path.dirname(DB_PATH)
    if not os.path.exists(db_dir):
        os.makedirs(db_dir)
    return DB_PATH


@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Initialize the database schema."""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Workouts table - stores both SugarWOD imports and manual entries
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS workouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date DATE NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                score_type TEXT,
                result_raw INTEGER,
                result_display TEXT,
                barbell_lift TEXT,
                set_details TEXT,
                notes TEXT,
                rx_or_scaled TEXT,
                is_pr BOOLEAN DEFAULT FALSE,
                source TEXT DEFAULT 'manual',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date, title, result_display)
            )
        """)
        
        # Lift PRs table - tracks personal records for each lift
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lift_prs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lift_name TEXT NOT NULL,
                weight REAL NOT NULL,
                reps INTEGER DEFAULT 1,
                date DATE NOT NULL,
                workout_id INTEGER,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (workout_id) REFERENCES workouts(id)
            )
        """)
        
        # Daily protein log
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS protein_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date DATE NOT NULL UNIQUE,
                grams INTEGER NOT NULL,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Body weight log
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS weight_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date DATE NOT NULL UNIQUE,
                weight REAL NOT NULL,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Readiness check-ins
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS readiness_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date DATE NOT NULL UNIQUE,
                sleep_quality INTEGER CHECK(sleep_quality BETWEEN 1 AND 5),
                energy INTEGER CHECK(energy BETWEEN 1 AND 5),
                soreness INTEGER CHECK(soreness BETWEEN 1 AND 5),
                stress INTEGER CHECK(stress BETWEEN 1 AND 5),
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Goals table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                target_value REAL,
                target_date DATE,
                achieved BOOLEAN DEFAULT FALSE,
                achieved_date DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Programs table - training blocks
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS programs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                start_date DATE NOT NULL,
                end_date DATE,
                is_active BOOLEAN DEFAULT TRUE,
                program_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Mobility log
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mobility_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date DATE NOT NULL,
                duration_minutes INTEGER NOT NULL,
                focus_area TEXT,
                exercises TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()


# Initialize database on module load
init_db()


# ============================================================================
# Enums and Input Models
# ============================================================================

class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


class ScoreType(str, Enum):
    TIME = "time"
    REPS = "reps"
    ROUNDS = "rounds"
    LOAD = "load"
    DISTANCE = "distance"
    OTHER = "other"


# ============================================================================
# SugarWOD Import
# ============================================================================

class ImportSugarWODInput(BaseModel):
    """Input for importing SugarWOD CSV export."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    file_path: str = Field(..., description="Path to the SugarWOD CSV export file")


def parse_sugarwod_date(date_str: str) -> str:
    """Parse SugarWOD date format (MM/DD/YYYY) to ISO format."""
    try:
        dt = datetime.strptime(date_str, "%m/%d/%Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return date_str


@mcp.tool(
    name="fitness_import_sugarwod",
    annotations={
        "title": "Import SugarWOD Export",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_import_sugarwod(params: ImportSugarWODInput) -> str:
    """Import workout data from a SugarWOD CSV export.
    
    Parses the CSV file, deduplicates against existing records, and imports
    new workouts. Also extracts lift PRs from barbell lift entries.
    
    Args:
        params: ImportSugarWODInput containing file_path
        
    Returns:
        str: Summary of imported records
    """
    file_path = params.file_path
    
    if not os.path.exists(file_path):
        return f"Error: File not found at {file_path}"
    
    imported = 0
    skipped = 0
    prs_added = 0
    errors = []
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        with open(file_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row in reader:
                try:
                    date_iso = parse_sugarwod_date(row.get('date', ''))
                    title = row.get('title', '').strip()
                    result_display = row.get('best_result_display', '').strip()
                    
                    # Check for duplicate
                    cursor.execute("""
                        SELECT id FROM workouts 
                        WHERE date = ? AND title = ? AND result_display = ?
                    """, (date_iso, title, result_display))
                    
                    if cursor.fetchone():
                        skipped += 1
                        continue
                    
                    # Parse result_raw
                    result_raw = None
                    raw_val = row.get('best_result_raw', '')
                    if raw_val:
                        try:
                            result_raw = int(float(raw_val))
                        except ValueError:
                            pass
                    
                    # Insert workout
                    cursor.execute("""
                        INSERT INTO workouts (
                            date, title, description, score_type, result_raw,
                            result_display, barbell_lift, set_details, notes,
                            rx_or_scaled, is_pr, source
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sugarwod')
                    """, (
                        date_iso,
                        title,
                        row.get('description', ''),
                        row.get('score_type', ''),
                        result_raw,
                        result_display,
                        row.get('barbell_lift', ''),
                        row.get('set_details', ''),
                        row.get('notes', ''),
                        row.get('rx_or_scaled', ''),
                        row.get('pr', '') == 'PR'
                    ))
                    
                    workout_id = cursor.lastrowid
                    imported += 1
                    
                    # Extract lift PR if applicable
                    barbell_lift = row.get('barbell_lift', '').strip()
                    if barbell_lift and row.get('score_type') == 'Load':
                        try:
                            weight = float(result_display)
                            # Check if this is actually a PR
                            cursor.execute("""
                                SELECT MAX(weight) FROM lift_prs
                                WHERE lift_name = ? AND reps = 1
                            """, (barbell_lift,))
                            max_weight = cursor.fetchone()[0]
                            
                            if max_weight is None or weight > max_weight:
                                cursor.execute("""
                                    INSERT INTO lift_prs (lift_name, weight, reps, date, workout_id)
                                    VALUES (?, ?, 1, ?, ?)
                                """, (barbell_lift, weight, date_iso, workout_id))
                                prs_added += 1
                        except ValueError:
                            pass
                    
                except Exception as e:
                    errors.append(f"Row error: {str(e)}")
        
        conn.commit()
    
    result = f"## SugarWOD Import Complete\n\n"
    result += f"- **Imported:** {imported} workouts\n"
    result += f"- **Skipped (duplicates):** {skipped}\n"
    result += f"- **Lift PRs recorded:** {prs_added}\n"
    
    if errors:
        result += f"\n### Errors ({len(errors)})\n"
        for err in errors[:5]:
            result += f"- {err}\n"
        if len(errors) > 5:
            result += f"- ... and {len(errors) - 5} more\n"
    
    return result


# ============================================================================
# Workout Logging
# ============================================================================

class LogWorkoutInput(BaseModel):
    """Input for logging a workout."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    title: str = Field(..., description="Workout name/title", min_length=1, max_length=200)
    date: Optional[str] = Field(default=None, description="Date (YYYY-MM-DD), defaults to today")
    description: Optional[str] = Field(default=None, description="Workout description/prescription")
    score_type: Optional[str] = Field(default=None, description="Type of score: time, reps, rounds, load, distance")
    result: Optional[str] = Field(default=None, description="Your result (e.g., '15:30', '225', '5 rounds + 10')")
    notes: Optional[str] = Field(default=None, description="Notes about the workout")
    rx: bool = Field(default=True, description="Did you do it as prescribed (RX)?")


@mcp.tool(
    name="fitness_log_workout",
    annotations={
        "title": "Log Workout",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False
    }
)
async def fitness_log_workout(params: LogWorkoutInput) -> str:
    """Log a completed workout.
    
    Records workout details including what you did, your score/result, and any notes.
    
    Args:
        params: LogWorkoutInput with workout details
        
    Returns:
        str: Confirmation of logged workout
    """
    workout_date = params.date or date.today().isoformat()
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO workouts (
                date, title, description, score_type, result_display,
                notes, rx_or_scaled, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'manual')
        """, (
            workout_date,
            params.title,
            params.description,
            params.score_type,
            params.result,
            params.notes,
            'RX' if params.rx else 'SCALED'
        ))
        
        conn.commit()
    
    return f"✅ Logged: **{params.title}** on {workout_date}\n- Result: {params.result or 'N/A'}\n- {'RX' if params.rx else 'Scaled'}"


# ============================================================================
# Workout Update and List
# ============================================================================

class ListWorkoutsInput(BaseModel):
    """Input for listing recent workouts."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    limit: int = Field(default=10, description="Number of workouts to return", ge=1, le=50)
    date: Optional[str] = Field(default=None, description="Filter by date (YYYY-MM-DD)")


@mcp.tool(
    name="fitness_list_workouts",
    annotations={
        "title": "List Recent Workouts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_list_workouts(params: ListWorkoutsInput) -> str:
    """List recent workouts with their IDs.
    
    Use this to find workout IDs for updating or deleting.
    
    Args:
        params: ListWorkoutsInput with limit and optional date filter
        
    Returns:
        str: List of workouts with IDs
    """
    with get_db() as conn:
        cursor = conn.cursor()
        
        if params.date:
            cursor.execute("""
                SELECT id, date, title, result_display, source
                FROM workouts
                WHERE date = ?
                ORDER BY id DESC
                LIMIT ?
            """, (params.date, params.limit))
        else:
            cursor.execute("""
                SELECT id, date, title, result_display, source
                FROM workouts
                ORDER BY date DESC, id DESC
                LIMIT ?
            """, (params.limit,))
        
        workouts = cursor.fetchall()
    
    if not workouts:
        return "No workouts found."
    
    result = "## Recent Workouts\n\n"
    result += "| ID | Date | Title | Result | Source |\n"
    result += "|-----|------|-------|--------|--------|\n"
    
    for w in workouts:
        result += f"| {w['id']} | {w['date']} | {w['title']} | {w['result_display'] or '-'} | {w['source']} |\n"
    
    return result


class UpdateWorkoutInput(BaseModel):
    """Input for updating a workout."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    workout_id: int = Field(..., description="ID of the workout to update", ge=1)
    date: Optional[str] = Field(default=None, description="New date (YYYY-MM-DD)")
    title: Optional[str] = Field(default=None, description="New title", max_length=200)
    description: Optional[str] = Field(default=None, description="New description")
    result: Optional[str] = Field(default=None, description="New result")
    notes: Optional[str] = Field(default=None, description="New notes")


@mcp.tool(
    name="fitness_update_workout",
    annotations={
        "title": "Update Workout",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_update_workout(params: UpdateWorkoutInput) -> str:
    """Update an existing workout.
    
    Only provided fields will be updated; others remain unchanged.
    Use fitness_list_workouts to find the workout ID.
    
    Args:
        params: UpdateWorkoutInput with workout_id and fields to update
        
    Returns:
        str: Confirmation of update
    """
    updates = []
    values = []
    
    if params.date is not None:
        updates.append("date = ?")
        values.append(params.date)
    if params.title is not None:
        updates.append("title = ?")
        values.append(params.title)
    if params.description is not None:
        updates.append("description = ?")
        values.append(params.description)
    if params.result is not None:
        updates.append("result_display = ?")
        values.append(params.result)
    if params.notes is not None:
        updates.append("notes = ?")
        values.append(params.notes)
    
    if not updates:
        return "No fields to update provided."
    
    values.append(params.workout_id)
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Check workout exists
        cursor.execute("SELECT id, title, date FROM workouts WHERE id = ?", (params.workout_id,))
        workout = cursor.fetchone()
        
        if not workout:
            return f"Error: Workout with ID {params.workout_id} not found."
        
        cursor.execute(f"""
            UPDATE workouts
            SET {', '.join(updates)}
            WHERE id = ?
        """, values)
        
        conn.commit()
    
    return f"✅ Updated workout #{params.workout_id} ({workout['title']} on {workout['date']})"


class DeleteWorkoutInput(BaseModel):
    """Input for deleting a workout."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    workout_id: int = Field(..., description="ID of the workout to delete", ge=1)
    confirm: bool = Field(..., description="Must be True to confirm deletion")


@mcp.tool(
    name="fitness_delete_workout",
    annotations={
        "title": "Delete Workout",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_delete_workout(params: DeleteWorkoutInput) -> str:
    """Delete a workout.
    
    Use fitness_list_workouts to find the workout ID.
    Requires confirm=True to proceed.
    
    Args:
        params: DeleteWorkoutInput with workout_id and confirm flag
        
    Returns:
        str: Confirmation of deletion
    """
    if not params.confirm:
        return "Deletion not confirmed. Set confirm=True to delete."
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Check workout exists
        cursor.execute("SELECT id, title, date FROM workouts WHERE id = ?", (params.workout_id,))
        workout = cursor.fetchone()
        
        if not workout:
            return f"Error: Workout with ID {params.workout_id} not found."
        
        cursor.execute("DELETE FROM workouts WHERE id = ?", (params.workout_id,))
        conn.commit()
    
    return f"🗑️ Deleted workout #{params.workout_id} ({workout['title']} on {workout['date']})"


# ============================================================================
# Lift Logging
# ============================================================================

class LogLiftInput(BaseModel):
    """Input for logging a lift."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    lift_name: str = Field(..., description="Name of lift (e.g., 'Deadlift', 'Back Squat', 'Bench Press')")
    weight: float = Field(..., description="Weight lifted in pounds", gt=0)
    reps: int = Field(default=1, description="Number of reps", ge=1)
    sets: int = Field(default=1, description="Number of sets completed", ge=1)
    date: Optional[str] = Field(default=None, description="Date (YYYY-MM-DD), defaults to today")
    notes: Optional[str] = Field(default=None, description="Notes about the lift")


@mcp.tool(
    name="fitness_log_lift",
    annotations={
        "title": "Log Lift",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False
    }
)
async def fitness_log_lift(params: LogLiftInput) -> str:
    """Log a strength lift and check for PRs.
    
    Records the lift and automatically checks if it's a new personal record.
    
    Args:
        params: LogLiftInput with lift details
        
    Returns:
        str: Confirmation including PR status if applicable
    """
    lift_date = params.date or date.today().isoformat()
    is_pr = False
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Check for PR at this rep range
        cursor.execute("""
            SELECT MAX(weight) FROM lift_prs
            WHERE lift_name = ? AND reps = ?
        """, (params.lift_name, params.reps))
        
        max_weight = cursor.fetchone()[0]
        
        if max_weight is None or params.weight > max_weight:
            is_pr = True
            cursor.execute("""
                INSERT INTO lift_prs (lift_name, weight, reps, date, notes)
                VALUES (?, ?, ?, ?, ?)
            """, (params.lift_name, params.weight, params.reps, lift_date, params.notes))
        
        # Also log as a workout
        cursor.execute("""
            INSERT INTO workouts (
                date, title, score_type, result_display, barbell_lift,
                notes, rx_or_scaled, is_pr, source
            ) VALUES (?, ?, 'Load', ?, ?, ?, 'RX', ?, 'manual')
        """, (
            lift_date,
            f"{params.lift_name} {params.sets}x{params.reps}",
            str(params.weight),
            params.lift_name,
            params.notes,
            is_pr
        ))
        
        conn.commit()
    
    result = f"✅ Logged: **{params.lift_name}** - {params.weight} lbs x {params.reps} reps"
    if params.sets > 1:
        result += f" ({params.sets} sets)"
    result += f" on {lift_date}"
    
    if is_pr:
        result += f"\n\n🎉 **NEW PR!** Previous best: {max_weight or 'None'} lbs"
    
    return result


# ============================================================================
# Protein Logging
# ============================================================================

class LogProteinInput(BaseModel):
    """Input for logging daily protein."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    grams: int = Field(..., description="Grams of protein consumed", ge=0, le=500)
    date: Optional[str] = Field(default=None, description="Date (YYYY-MM-DD), defaults to today")
    notes: Optional[str] = Field(default=None, description="Notes (e.g., what you ate)")


class AddProteinInput(BaseModel):
    """Input for adding protein to today's total."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    grams: int = Field(..., description="Grams of protein to add", ge=0, le=300)
    food: Optional[str] = Field(default=None, description="What food this came from")


@mcp.tool(
    name="fitness_log_protein",
    annotations={
        "title": "Log Daily Protein",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_log_protein(params: LogProteinInput) -> str:
    """Log total daily protein intake.
    
    Sets the protein total for a given day. Use fitness_add_protein to
    incrementally add throughout the day.
    
    Args:
        params: LogProteinInput with grams and optional date
        
    Returns:
        str: Confirmation with progress toward goal
    """
    protein_date = params.date or date.today().isoformat()
    target = 160  # Target grams per day
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO protein_log (date, grams, notes)
            VALUES (?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET grams = ?, notes = ?
        """, (protein_date, params.grams, params.notes, params.grams, params.notes))
        
        conn.commit()
    
    pct = round(params.grams / target * 100)
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    
    return f"✅ Protein logged: **{params.grams}g** on {protein_date}\n\nProgress: [{bar}] {pct}% of {target}g goal"


@mcp.tool(
    name="fitness_add_protein",
    annotations={
        "title": "Add Protein to Today",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False
    }
)
async def fitness_add_protein(params: AddProteinInput) -> str:
    """Add protein to today's running total.
    
    Incrementally add protein as you eat throughout the day.
    
    Args:
        params: AddProteinInput with grams to add
        
    Returns:
        str: Updated daily total with progress
    """
    today = date.today().isoformat()
    target = 160
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Get current total
        cursor.execute("SELECT grams, notes FROM protein_log WHERE date = ?", (today,))
        row = cursor.fetchone()
        
        current = row['grams'] if row else 0
        current_notes = row['notes'] if row else ""
        
        new_total = current + params.grams
        
        # Append to notes
        new_note = params.food or f"+{params.grams}g"
        if current_notes:
            new_notes = f"{current_notes}; {new_note}"
        else:
            new_notes = new_note
        
        cursor.execute("""
            INSERT INTO protein_log (date, grams, notes)
            VALUES (?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET grams = ?, notes = ?
        """, (today, new_total, new_notes, new_total, new_notes))
        
        conn.commit()
    
    pct = round(new_total / target * 100)
    bar = "█" * min(pct // 10, 10) + "░" * max(10 - pct // 10, 0)
    
    result = f"✅ Added **{params.grams}g** protein"
    if params.food:
        result += f" ({params.food})"
    result += f"\n\n**Today's total: {new_total}g**\n[{bar}] {pct}% of {target}g goal"
    
    if new_total >= target:
        result += "\n\n🎯 Goal reached!"
    else:
        result += f"\n\n{target - new_total}g remaining"
    
    return result


class UpdateProteinInput(BaseModel):
    """Input for updating a protein log entry."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    date: str = Field(..., description="Date of the protein entry to update (YYYY-MM-DD)")
    grams: Optional[int] = Field(default=None, description="New grams value", ge=0, le=500)
    notes: Optional[str] = Field(default=None, description="New notes value")


@mcp.tool(
    name="fitness_update_protein",
    annotations={
        "title": "Update Protein Entry",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_update_protein(params: UpdateProteinInput) -> str:
    """Update a protein log entry for a specific date.
    
    Only provided fields will be updated; others remain unchanged.
    
    Args:
        params: UpdateProteinInput with date and fields to update
        
    Returns:
        str: Confirmation of update
    """
    updates = []
    values = []
    
    if params.grams is not None:
        updates.append("grams = ?")
        values.append(params.grams)
    if params.notes is not None:
        updates.append("notes = ?")
        values.append(params.notes)
    
    if not updates:
        return "No fields to update provided."
    
    values.append(params.date)
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Check entry exists
        cursor.execute("SELECT * FROM protein_log WHERE date = ?", (params.date,))
        entry = cursor.fetchone()
        
        if not entry:
            return f"No protein entry found for {params.date}"
        
        cursor.execute(f"""
            UPDATE protein_log
            SET {', '.join(updates)}
            WHERE date = ?
        """, values)
        
        # Get updated entry
        cursor.execute("SELECT grams, notes FROM protein_log WHERE date = ?", (params.date,))
        updated = cursor.fetchone()
        
        conn.commit()
    
    target = 160
    pct = round(updated['grams'] / target * 100)
    bar = "█" * min(pct // 10, 10) + "░" * max(10 - pct // 10, 0)
    
    return f"✅ Updated protein for {params.date}: **{updated['grams']}g**\n[{bar}] {pct}% of {target}g goal"


# ============================================================================
# Weight Logging
# ============================================================================

class LogWeightInput(BaseModel):
    """Input for logging body weight."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    weight: float = Field(..., description="Body weight in pounds", gt=50, lt=500)
    date: Optional[str] = Field(default=None, description="Date (YYYY-MM-DD), defaults to today")
    notes: Optional[str] = Field(default=None, description="Notes")


@mcp.tool(
    name="fitness_log_weight",
    annotations={
        "title": "Log Body Weight",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_log_weight(params: LogWeightInput) -> str:
    """Log body weight.
    
    Track your weight over time. Weekly weigh-ins recommended for tracking trends.
    
    Args:
        params: LogWeightInput with weight
        
    Returns:
        str: Confirmation with trend info if available
    """
    weight_date = params.date or date.today().isoformat()
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO weight_log (date, weight, notes)
            VALUES (?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET weight = ?, notes = ?
        """, (weight_date, params.weight, params.notes, params.weight, params.notes))
        
        # Get trend
        cursor.execute("""
            SELECT weight, date FROM weight_log
            WHERE date < ?
            ORDER BY date DESC LIMIT 1
        """, (weight_date,))
        
        prev = cursor.fetchone()
        conn.commit()
    
    result = f"✅ Weight logged: **{params.weight} lbs** on {weight_date}"
    
    if prev:
        diff = params.weight - prev['weight']
        direction = "↑" if diff > 0 else "↓" if diff < 0 else "→"
        result += f"\n\nChange from {prev['date']}: {direction} {abs(diff):.1f} lbs"
    
    return result


# ============================================================================
# Readiness Logging
# ============================================================================

class LogReadinessInput(BaseModel):
    """Input for logging daily readiness."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    sleep_quality: int = Field(..., description="Sleep quality 1-5 (5=great)", ge=1, le=5)
    energy: int = Field(..., description="Energy level 1-5 (5=high)", ge=1, le=5)
    soreness: int = Field(..., description="Muscle soreness 1-5 (5=very sore)", ge=1, le=5)
    stress: int = Field(..., description="Stress level 1-5 (5=very stressed)", ge=1, le=5)
    date: Optional[str] = Field(default=None, description="Date (YYYY-MM-DD), defaults to today")
    notes: Optional[str] = Field(default=None, description="Notes about how you're feeling")


@mcp.tool(
    name="fitness_log_readiness",
    annotations={
        "title": "Log Daily Readiness",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_log_readiness(params: LogReadinessInput) -> str:
    """Log daily readiness check-in.
    
    Quick morning check-in to track recovery and readiness to train.
    
    Args:
        params: LogReadinessInput with readiness scores
        
    Returns:
        str: Readiness summary with training recommendation
    """
    readiness_date = params.date or date.today().isoformat()
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO readiness_log (date, sleep_quality, energy, soreness, stress, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET 
                sleep_quality = ?, energy = ?, soreness = ?, stress = ?, notes = ?
        """, (
            readiness_date, params.sleep_quality, params.energy, params.soreness, params.stress, params.notes,
            params.sleep_quality, params.energy, params.soreness, params.stress, params.notes
        ))
        
        conn.commit()
    
    # Calculate readiness score (higher is better, soreness and stress are inverted)
    readiness_score = (params.sleep_quality + params.energy + (6 - params.soreness) + (6 - params.stress)) / 4
    
    # Training recommendation
    if readiness_score >= 4:
        rec = "💪 **Go hard** - You're well recovered, push it today"
    elif readiness_score >= 3:
        rec = "✅ **Normal training** - Good to go with planned workout"
    elif readiness_score >= 2:
        rec = "⚠️ **Modify** - Consider lighter weights or shorter session"
    else:
        rec = "🛑 **Rest or light movement** - Prioritize recovery today"
    
    result = f"## Readiness Check-in: {readiness_date}\n\n"
    result += f"| Metric | Score |\n|--------|-------|\n"
    result += f"| Sleep | {'⭐' * params.sleep_quality}{'☆' * (5 - params.sleep_quality)} |\n"
    result += f"| Energy | {'⭐' * params.energy}{'☆' * (5 - params.energy)} |\n"
    result += f"| Soreness | {'⭐' * params.soreness}{'☆' * (5 - params.soreness)} |\n"
    result += f"| Stress | {'⭐' * params.stress}{'☆' * (5 - params.stress)} |\n\n"
    result += f"**Readiness Score:** {readiness_score:.1f}/5\n\n"
    result += rec
    
    if params.notes:
        result += f"\n\nNotes: {params.notes}"
    
    return result


class DeleteReadinessInput(BaseModel):
    """Input for deleting a readiness entry."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    date: str = Field(..., description="Date of the readiness entry to delete (YYYY-MM-DD)")
    confirm: bool = Field(..., description="Must be True to confirm deletion")


@mcp.tool(
    name="fitness_delete_readiness",
    annotations={
        "title": "Delete Readiness Entry",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_delete_readiness(params: DeleteReadinessInput) -> str:
    """Delete a readiness entry by date.
    
    Args:
        params: DeleteReadinessInput with date and confirm flag
        
    Returns:
        str: Confirmation of deletion
    """
    if not params.confirm:
        return "Deletion not confirmed. Set confirm=True to delete."
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM readiness_log WHERE date = ?", (params.date,))
        entry = cursor.fetchone()
        
        if not entry:
            return f"No readiness entry found for {params.date}"
        
        cursor.execute("DELETE FROM readiness_log WHERE date = ?", (params.date,))
        conn.commit()
    
    return f"🗑️ Deleted readiness entry for {params.date}"


# ============================================================================
# Mobility Logging
# ============================================================================

class LogMobilityInput(BaseModel):
    """Input for logging mobility work."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    duration_minutes: int = Field(..., description="Duration in minutes", ge=1, le=120)
    focus_area: Optional[str] = Field(default=None, description="Focus area (e.g., 'hips', 'ankles', 'shoulders')")
    exercises: Optional[str] = Field(default=None, description="Exercises performed")
    date: Optional[str] = Field(default=None, description="Date (YYYY-MM-DD), defaults to today")
    notes: Optional[str] = Field(default=None, description="Notes")


@mcp.tool(
    name="fitness_log_mobility",
    annotations={
        "title": "Log Mobility Work",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False
    }
)
async def fitness_log_mobility(params: LogMobilityInput) -> str:
    """Log mobility/stretching work.
    
    Track your mobility sessions to build consistency.
    
    Args:
        params: LogMobilityInput with mobility session details
        
    Returns:
        str: Confirmation with weekly mobility stats
    """
    mobility_date = params.date or date.today().isoformat()
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO mobility_log (date, duration_minutes, focus_area, exercises, notes)
            VALUES (?, ?, ?, ?, ?)
        """, (mobility_date, params.duration_minutes, params.focus_area, params.exercises, params.notes))
        
        # Get weekly stats
        week_ago = (datetime.strptime(mobility_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
        cursor.execute("""
            SELECT COUNT(*) as sessions, SUM(duration_minutes) as total_mins
            FROM mobility_log WHERE date >= ?
        """, (week_ago,))
        
        stats = cursor.fetchone()
        conn.commit()
    
    result = f"✅ Logged: **{params.duration_minutes} min** mobility work on {mobility_date}"
    if params.focus_area:
        result += f"\n- Focus: {params.focus_area}"
    if params.exercises:
        result += f"\n- Exercises: {params.exercises}"
    
    result += f"\n\n**This week:** {stats['sessions']} sessions, {stats['total_mins'] or 0} total minutes"
    
    return result


# ============================================================================
# Program Management
# ============================================================================

# The 4-week starter program
STARTER_PROGRAM = {
    "name": "4-Week Muscle Building Starter",
    "description": "Foundation program: 4 days lifting, 1 day conditioning, focus on compound movements with mobility work",
    "weeks": [
        {
            "week": 1,
            "theme": "Foundation",
            "days": {
                "Monday": {
                    "name": "Upper Push",
                    "exercises": [
                        {"name": "Bench Press", "sets": 3, "reps": 8, "notes": "Moderate weight, leave 2-3 reps in tank"},
                        {"name": "DB Shoulder Press", "sets": 3, "reps": 10},
                        {"name": "Incline DB Press", "sets": 3, "reps": 10},
                        {"name": "Tricep Pushdown", "sets": 3, "reps": 12},
                        {"name": "Lateral Raises", "sets": 3, "reps": 15},
                    ],
                    "mobility": "Hip mobility: 5 min (couch stretch, 90/90)"
                },
                "Tuesday": {
                    "name": "Lower",
                    "exercises": [
                        {"name": "Goblet Squat", "sets": 3, "reps": 10, "notes": "Focus on depth, build ankle mobility"},
                        {"name": "Romanian Deadlift", "sets": 3, "reps": 10},
                        {"name": "Leg Press", "sets": 3, "reps": 12},
                        {"name": "Walking Lunges", "sets": 2, "reps": "10 each leg"},
                        {"name": "Leg Curl", "sets": 3, "reps": 12},
                        {"name": "Calf Raises", "sets": 3, "reps": 15},
                    ],
                    "mobility": "Ankle mobility: 5 min (wall stretches, banded distractions)"
                },
                "Wednesday": {
                    "name": "Rest or Light Mobility",
                    "exercises": [],
                    "mobility": "Optional: 10-15 min full body stretch or yoga"
                },
                "Thursday": {
                    "name": "Upper Pull",
                    "exercises": [
                        {"name": "Barbell Row", "sets": 3, "reps": 8},
                        {"name": "Lat Pulldown", "sets": 3, "reps": 10},
                        {"name": "Seated Cable Row", "sets": 3, "reps": 10},
                        {"name": "Face Pulls", "sets": 3, "reps": 15},
                        {"name": "Barbell Curl", "sets": 3, "reps": 10},
                        {"name": "Hammer Curl", "sets": 2, "reps": 12},
                    ],
                    "mobility": "Hip mobility: 5 min"
                },
                "Friday": {
                    "name": "Full Body + Conditioning",
                    "exercises": [
                        {"name": "Deadlift", "sets": 3, "reps": 5, "notes": "Heavier - your strongest lift"},
                        {"name": "Push Press", "sets": 3, "reps": 5},
                        {"name": "Pull-ups or Assisted Pull-ups", "sets": 3, "reps": "max"},
                    ],
                    "conditioning": "15 min: Row 2K or Run 1 mile + 50 KB swings",
                    "mobility": "5 min cool down stretch"
                },
                "Saturday": {
                    "name": "Rest",
                    "exercises": [],
                    "mobility": "Optional: sauna, walk"
                },
                "Sunday": {
                    "name": "Rest or Active Recovery",
                    "exercises": [],
                    "mobility": "Optional: 20 min walk, light stretch"
                }
            }
        },
        {
            "week": 2,
            "theme": "Foundation (continued)",
            "days": "Same as Week 1 - focus on form and finding appropriate weights"
        },
        {
            "week": 3,
            "theme": "Progression",
            "notes": "Add 5 lbs to barbell movements if Week 1-2 felt manageable. Add 1 set to compound lifts (4x8 instead of 3x8).",
            "days": "Same structure as Week 1 with increased volume"
        },
        {
            "week": 4,
            "theme": "Progression (continued)",
            "notes": "Continue Week 3 progression. Try to improve conditioning time/distance slightly.",
            "days": "Same structure as Week 3"
        }
    ],
    "principles": [
        "Tempo: Control the weight down (2-3 sec), don't just drop it",
        "Rest: 90 sec to 2 min between sets - you're not doing metcons",
        "Progression: If you get all reps with good form, add weight next session",
        "Mobility is non-negotiable: 5 min per session built in"
    ]
}


class GetTodayInput(BaseModel):
    """Input for getting today's workout."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    date: Optional[str] = Field(default=None, description="Date to check (YYYY-MM-DD), defaults to today")


@mcp.tool(
    name="fitness_get_today",
    annotations={
        "title": "Get Today's Workout",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_get_today(params: GetTodayInput) -> str:
    """Get the prescribed workout for today based on active program.
    
    Returns what you should do today according to your training program,
    adjusted for your readiness if logged.
    
    Args:
        params: GetTodayInput with optional date
        
    Returns:
        str: Today's workout prescription
    """
    check_date = params.date or date.today().isoformat()
    dt = datetime.strptime(check_date, "%Y-%m-%d")
    day_name = dt.strftime("%A")
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Check for active program
        cursor.execute("""
            SELECT * FROM programs WHERE is_active = TRUE
            ORDER BY start_date DESC LIMIT 1
        """)
        program = cursor.fetchone()
        
        # Get today's readiness
        cursor.execute("""
            SELECT * FROM readiness_log WHERE date = ?
        """, (check_date,))
        readiness = cursor.fetchone()
        
        # Get today's protein
        cursor.execute("""
            SELECT grams FROM protein_log WHERE date = ?
        """, (check_date,))
        protein = cursor.fetchone()
    
    # Use default program if none active
    if program:
        program_data = json.loads(program['program_data'])
    else:
        program_data = STARTER_PROGRAM
    
    # Calculate which week we're in (default to week 1 if no start date)
    if program and program['start_date']:
        start = datetime.strptime(program['start_date'], "%Y-%m-%d")
        week_num = min(((dt - start).days // 7) + 1, 4)
    else:
        week_num = 1
    
    # Get today's workout
    week_data = program_data['weeks'][min(week_num - 1, len(program_data['weeks']) - 1)]
    
    if isinstance(week_data.get('days'), dict):
        day_workout = week_data['days'].get(day_name, {"name": "Rest", "exercises": []})
    else:
        # For weeks that reference week 1
        day_workout = program_data['weeks'][0]['days'].get(day_name, {"name": "Rest", "exercises": []})
    
    result = f"## {day_name}, {check_date}\n"
    result += f"### Week {week_num}: {week_data.get('theme', '')}\n\n"
    
    if week_data.get('notes'):
        result += f"*{week_data['notes']}*\n\n"
    
    result += f"## {day_workout['name']}\n\n"
    
    # Readiness adjustment
    if readiness:
        score = (readiness['sleep_quality'] + readiness['energy'] + 
                (6 - readiness['soreness']) + (6 - readiness['stress'])) / 4
        if score < 2.5:
            result += "⚠️ **Low readiness today** - consider reducing volume or intensity\n\n"
        elif score >= 4:
            result += "💪 **High readiness** - push it today!\n\n"
    
    # Exercises
    if day_workout.get('exercises'):
        result += "### Exercises\n\n"
        for ex in day_workout['exercises']:
            reps = ex.get('reps', '')
            result += f"- **{ex['name']}**: {ex['sets']} x {reps}"
            if ex.get('notes'):
                result += f" - *{ex['notes']}*"
            result += "\n"
    
    if day_workout.get('conditioning'):
        result += f"\n### Conditioning\n{day_workout['conditioning']}\n"
    
    if day_workout.get('mobility'):
        result += f"\n### Mobility\n{day_workout['mobility']}\n"
    
    # Protein check
    result += "\n---\n"
    if protein:
        result += f"📊 Protein so far: {protein['grams']}g / 160g"
    else:
        result += "📊 No protein logged yet today"
    
    return result


class SetProgramInput(BaseModel):
    """Input for setting the active program."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    start_date: Optional[str] = Field(default=None, description="Program start date (YYYY-MM-DD), defaults to next Monday")
    use_default: bool = Field(default=True, description="Use the default 4-week starter program")


@mcp.tool(
    name="fitness_set_program",
    annotations={
        "title": "Set Active Program",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_set_program(params: SetProgramInput) -> str:
    """Set the active training program.
    
    Activates a training program starting from the specified date.
    
    Args:
        params: SetProgramInput with start date and program selection
        
    Returns:
        str: Confirmation of program activation
    """
    # Calculate start date (next Monday if not specified)
    if params.start_date:
        start = datetime.strptime(params.start_date, "%Y-%m-%d")
    else:
        today = date.today()
        days_until_monday = (7 - today.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        start = today + timedelta(days=days_until_monday)
    
    start_str = start.strftime("%Y-%m-%d") if isinstance(start, date) else start.strftime("%Y-%m-%d")
    end_str = (datetime.strptime(start_str, "%Y-%m-%d") + timedelta(weeks=4)).strftime("%Y-%m-%d")
    
    program = STARTER_PROGRAM
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Deactivate existing programs
        cursor.execute("UPDATE programs SET is_active = FALSE")
        
        # Insert new program
        cursor.execute("""
            INSERT INTO programs (name, description, start_date, end_date, is_active, program_data)
            VALUES (?, ?, ?, ?, TRUE, ?)
        """, (
            program['name'],
            program['description'],
            start_str,
            end_str,
            json.dumps(program)
        ))
        
        conn.commit()
    
    result = f"## Program Activated! 🎯\n\n"
    result += f"**{program['name']}**\n\n"
    result += f"- Start: {start_str}\n"
    result += f"- End: {end_str}\n\n"
    result += "### Key Principles\n"
    for p in program['principles']:
        result += f"- {p}\n"
    
    result += f"\n\nUse `fitness_get_today` to see each day's workout!"
    
    return result


# ============================================================================
# Analytics and Reporting
# ============================================================================

class GetLiftHistoryInput(BaseModel):
    """Input for getting lift history."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    lift_name: str = Field(..., description="Name of lift to query")
    limit: int = Field(default=20, description="Number of records to return", ge=1, le=100)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


@mcp.tool(
    name="fitness_get_lift_history",
    annotations={
        "title": "Get Lift History",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_get_lift_history(params: GetLiftHistoryInput) -> str:
    """Get history for a specific lift.
    
    Shows progression over time for a given lift including PRs.
    
    Args:
        params: GetLiftHistoryInput with lift name
        
    Returns:
        str: Lift history with progression data
    """
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Get lift history from workouts
        cursor.execute("""
            SELECT date, result_display, notes, is_pr, set_details
            FROM workouts
            WHERE barbell_lift LIKE ?
            ORDER BY date DESC
            LIMIT ?
        """, (f"%{params.lift_name}%", params.limit))
        
        history = cursor.fetchall()
        
        # Get PR
        cursor.execute("""
            SELECT MAX(weight) as pr, date FROM lift_prs
            WHERE lift_name LIKE ?
        """, (f"%{params.lift_name}%",))
        
        pr_row = cursor.fetchone()
    
    if params.response_format == ResponseFormat.JSON:
        return json.dumps({
            "lift": params.lift_name,
            "pr": pr_row['pr'] if pr_row else None,
            "history": [dict(h) for h in history]
        }, indent=2)
    
    result = f"## {params.lift_name} History\n\n"
    
    if pr_row and pr_row['pr']:
        result += f"**Current PR: {pr_row['pr']} lbs**\n\n"
    
    if history:
        result += "| Date | Weight | Notes |\n|------|--------|-------|\n"
        for h in history:
            pr_marker = " 🏆" if h['is_pr'] else ""
            notes = (h['notes'] or "")[:30]
            result += f"| {h['date']} | {h['result_display']} lbs{pr_marker} | {notes} |\n"
    else:
        result += "No history found for this lift.\n"
    
    return result


class GetPRsInput(BaseModel):
    """Input for getting all PRs."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


@mcp.tool(
    name="fitness_get_prs",
    annotations={
        "title": "Get All PRs",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_get_prs(params: GetPRsInput) -> str:
    """Get all personal records.
    
    Returns your PR board showing best lifts across all movements.
    
    Args:
        params: GetPRsInput with format preference
        
    Returns:
        str: PR board
    """
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT lp.lift_name, lp.weight as pr, lp.date
            FROM lift_prs lp
            INNER JOIN (
                SELECT lift_name, MAX(weight) as max_weight
                FROM lift_prs
                GROUP BY lift_name
            ) max_prs ON lp.lift_name = max_prs.lift_name AND lp.weight = max_prs.max_weight
            GROUP BY lp.lift_name
            ORDER BY lp.lift_name
        """)
        
        prs = cursor.fetchall()
    
    if params.response_format == ResponseFormat.JSON:
        return json.dumps([dict(p) for p in prs], indent=2)
    
    result = "## 🏆 Personal Records\n\n"
    
    if prs:
        result += "| Lift | PR | Date |\n|------|-----|------|\n"
        for p in prs:
            result += f"| {p['lift_name']} | {p['pr']} lbs | {p['date']} |\n"
    else:
        result += "No PRs recorded yet. Start lifting!\n"
    
    return result


class WeeklyReviewInput(BaseModel):
    """Input for weekly review."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    weeks_back: int = Field(default=1, description="How many weeks back to review", ge=1, le=12)


@mcp.tool(
    name="fitness_weekly_review",
    annotations={
        "title": "Weekly Review",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_weekly_review(params: WeeklyReviewInput) -> str:
    """Generate a weekly training review.
    
    Summarizes workouts, protein adherence, weight trend, and readiness for the past week(s).
    
    Args:
        params: WeeklyReviewInput with weeks to review
        
    Returns:
        str: Comprehensive weekly review
    """
    end_date = date.today()
    start_date = end_date - timedelta(weeks=params.weeks_back)
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Workout count
        cursor.execute("""
            SELECT COUNT(*) as count FROM workouts
            WHERE date >= ? AND date <= ?
        """, (start_date.isoformat(), end_date.isoformat()))
        workout_count = cursor.fetchone()['count']
        
        # Protein adherence
        cursor.execute("""
            SELECT date, grams FROM protein_log
            WHERE date >= ? AND date <= ?
            ORDER BY date
        """, (start_date.isoformat(), end_date.isoformat()))
        protein_days = cursor.fetchall()
        
        # Weight trend
        cursor.execute("""
            SELECT date, weight FROM weight_log
            WHERE date >= ? AND date <= ?
            ORDER BY date
        """, (start_date.isoformat(), end_date.isoformat()))
        weights = cursor.fetchall()
        
        # Readiness average
        cursor.execute("""
            SELECT AVG(sleep_quality) as sleep, AVG(energy) as energy,
                   AVG(soreness) as soreness, AVG(stress) as stress
            FROM readiness_log
            WHERE date >= ? AND date <= ?
        """, (start_date.isoformat(), end_date.isoformat()))
        readiness_avg = cursor.fetchone()
        
        # Mobility
        cursor.execute("""
            SELECT COUNT(*) as sessions, SUM(duration_minutes) as total
            FROM mobility_log
            WHERE date >= ? AND date <= ?
        """, (start_date.isoformat(), end_date.isoformat()))
        mobility = cursor.fetchone()
        
        # Recent PRs
        cursor.execute("""
            SELECT lift_name, weight, date FROM lift_prs
            WHERE date >= ? AND date <= ?
            ORDER BY date DESC
        """, (start_date.isoformat(), end_date.isoformat()))
        recent_prs = cursor.fetchall()
    
    result = f"## Weekly Review: {start_date} to {end_date}\n\n"
    
    # Workouts
    result += f"### 💪 Training\n"
    result += f"- **Workouts completed:** {workout_count}\n"
    if workout_count >= 4:
        result += "  ✅ Great consistency!\n"
    elif workout_count >= 2:
        result += "  ⚠️ Room for improvement\n"
    else:
        result += "  ❌ Need more sessions\n"
    
    # Protein
    result += f"\n### 🥩 Protein\n"
    if protein_days:
        protein_target = 160
        days_hit = sum(1 for p in protein_days if p['grams'] >= protein_target)
        avg_protein = sum(p['grams'] for p in protein_days) / len(protein_days)
        result += f"- **Days logged:** {len(protein_days)}\n"
        result += f"- **Days hitting 160g+:** {days_hit}\n"
        result += f"- **Average:** {avg_protein:.0f}g/day\n"
    else:
        result += "- No protein logged this week\n"
    
    # Weight
    result += f"\n### ⚖️ Weight\n"
    if weights:
        first_weight = weights[0]['weight']
        last_weight = weights[-1]['weight']
        diff = last_weight - first_weight
        result += f"- **Start:** {first_weight} lbs\n"
        result += f"- **End:** {last_weight} lbs\n"
        result += f"- **Change:** {'+' if diff > 0 else ''}{diff:.1f} lbs\n"
    else:
        result += "- No weight logged this week\n"
    
    # Readiness
    result += f"\n### 😴 Recovery (averages)\n"
    if readiness_avg and readiness_avg['sleep']:
        result += f"- Sleep: {readiness_avg['sleep']:.1f}/5\n"
        result += f"- Energy: {readiness_avg['energy']:.1f}/5\n"
        result += f"- Soreness: {readiness_avg['soreness']:.1f}/5\n"
        result += f"- Stress: {readiness_avg['stress']:.1f}/5\n"
    else:
        result += "- No readiness check-ins this week\n"
    
    # Mobility
    result += f"\n### 🧘 Mobility\n"
    result += f"- **Sessions:** {mobility['sessions'] or 0}\n"
    result += f"- **Total time:** {mobility['total'] or 0} minutes\n"
    
    # PRs
    if recent_prs:
        result += f"\n### 🏆 New PRs!\n"
        for pr in recent_prs:
            result += f"- {pr['lift_name']}: {pr['weight']} lbs ({pr['date']})\n"
    
    return result


class GetSummaryInput(BaseModel):
    """Input for dashboard summary."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')


@mcp.tool(
    name="fitness_get_summary",
    annotations={
        "title": "Get Dashboard Summary",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_get_summary(params: GetSummaryInput) -> str:
    """Get a quick dashboard summary.
    
    Overview of current status: today's workout, protein, recent activity.
    
    Args:
        params: GetSummaryInput (empty)
        
    Returns:
        str: Dashboard summary
    """
    today = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Today's protein
        cursor.execute("SELECT grams FROM protein_log WHERE date = ?", (today,))
        protein = cursor.fetchone()
        
        # This week's workouts
        cursor.execute("""
            SELECT COUNT(*) as count FROM workouts WHERE date >= ?
        """, (week_ago,))
        week_workouts = cursor.fetchone()['count']
        
        # Latest weight
        cursor.execute("""
            SELECT weight, date FROM weight_log ORDER BY date DESC LIMIT 1
        """)
        latest_weight = cursor.fetchone()
        
        # Today's readiness
        cursor.execute("SELECT * FROM readiness_log WHERE date = ?", (today,))
        readiness = cursor.fetchone()
        
        # Active program
        cursor.execute("SELECT name, start_date FROM programs WHERE is_active = TRUE LIMIT 1")
        program = cursor.fetchone()
    
    result = f"## Fitness Dashboard: {today}\n\n"
    
    # Program status
    if program:
        result += f"📋 **Program:** {program['name']} (started {program['start_date']})\n\n"
    else:
        result += "📋 **No active program** - use `fitness_set_program` to start\n\n"
    
    # Quick stats
    result += "### Today\n"
    result += f"- Protein: {protein['grams'] if protein else 0}g / 160g\n"
    
    if readiness:
        score = (readiness['sleep_quality'] + readiness['energy'] + 
                (6 - readiness['soreness']) + (6 - readiness['stress'])) / 4
        result += f"- Readiness: {score:.1f}/5\n"
    else:
        result += "- Readiness: not logged\n"
    
    result += f"\n### This Week\n"
    result += f"- Workouts: {week_workouts}\n"
    
    if latest_weight:
        result += f"- Latest weight: {latest_weight['weight']} lbs ({latest_weight['date']})\n"
    
    return result


# ============================================================================
# Data Query Tools (for analysis and charting)
# ============================================================================

class GetReadinessHistoryInput(BaseModel):
    """Input for getting readiness history."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    days_back: int = Field(default=30, description="Number of days to look back", ge=1, le=365)
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)


@mcp.tool(
    name="fitness_get_readiness_history",
    annotations={
        "title": "Get Readiness History",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_get_readiness_history(params: GetReadinessHistoryInput) -> str:
    """Get readiness check-in history.
    
    Returns sleep quality, energy, soreness, and stress over time.
    Useful for charting trends.
    
    Args:
        params: GetReadinessHistoryInput with days_back
        
    Returns:
        str: Readiness data as JSON or markdown
    """
    start_date = (date.today() - timedelta(days=params.days_back)).isoformat()
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT date, sleep_quality, energy, soreness, stress, notes
            FROM readiness_log
            WHERE date >= ?
            ORDER BY date ASC
        """, (start_date,))
        rows = cursor.fetchall()
    
    data = [dict(row) for row in rows]
    
    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2)
    
    if not data:
        return "No readiness data found for this period."
    
    result = "## Readiness History\n\n"
    result += "| Date | Sleep | Energy | Soreness | Stress |\n"
    result += "|------|-------|--------|----------|--------|\n"
    for r in data:
        result += f"| {r['date']} | {r['sleep_quality']} | {r['energy']} | {r['soreness']} | {r['stress']} |\n"
    
    return result


class GetProteinHistoryInput(BaseModel):
    """Input for getting protein history."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    days_back: int = Field(default=30, description="Number of days to look back", ge=1, le=365)
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)


@mcp.tool(
    name="fitness_get_protein_history",
    annotations={
        "title": "Get Protein History",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_get_protein_history(params: GetProteinHistoryInput) -> str:
    """Get protein intake history.
    
    Returns daily protein totals over time.
    
    Args:
        params: GetProteinHistoryInput with days_back
        
    Returns:
        str: Protein data as JSON or markdown
    """
    start_date = (date.today() - timedelta(days=params.days_back)).isoformat()
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT date, grams, notes
            FROM protein_log
            WHERE date >= ?
            ORDER BY date ASC
        """, (start_date,))
        rows = cursor.fetchall()
    
    data = [dict(row) for row in rows]
    
    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2)
    
    if not data:
        return "No protein data found for this period."
    
    result = "## Protein History\n\n"
    result += "| Date | Grams | Notes |\n"
    result += "|------|-------|-------|\n"
    for r in data:
        notes = (r['notes'] or "")[:40]
        result += f"| {r['date']} | {r['grams']}g | {notes} |\n"
    
    return result


class GetWeightHistoryInput(BaseModel):
    """Input for getting weight history."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    days_back: int = Field(default=90, description="Number of days to look back", ge=1, le=365)
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)


@mcp.tool(
    name="fitness_get_weight_history",
    annotations={
        "title": "Get Weight History",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_get_weight_history(params: GetWeightHistoryInput) -> str:
    """Get body weight history.
    
    Returns weight measurements over time.
    
    Args:
        params: GetWeightHistoryInput with days_back
        
    Returns:
        str: Weight data as JSON or markdown
    """
    start_date = (date.today() - timedelta(days=params.days_back)).isoformat()
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT date, weight, notes
            FROM weight_log
            WHERE date >= ?
            ORDER BY date ASC
        """, (start_date,))
        rows = cursor.fetchall()
    
    data = [dict(row) for row in rows]
    
    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2)
    
    if not data:
        return "No weight data found for this period."
    
    result = "## Weight History\n\n"
    result += "| Date | Weight (lbs) |\n"
    result += "|------|-------------|\n"
    for r in data:
        result += f"| {r['date']} | {r['weight']} |\n"
    
    return result


class GetWorkoutHistoryInput(BaseModel):
    """Input for getting workout history."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    days_back: int = Field(default=30, description="Number of days to look back", ge=1, le=365)
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)


@mcp.tool(
    name="fitness_get_workout_history",
    annotations={
        "title": "Get Workout History",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_get_workout_history(params: GetWorkoutHistoryInput) -> str:
    """Get workout history.
    
    Returns all workouts over time with details.
    
    Args:
        params: GetWorkoutHistoryInput with days_back
        
    Returns:
        str: Workout data as JSON or markdown
    """
    start_date = (date.today() - timedelta(days=params.days_back)).isoformat()
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, date, title, description, score_type, result_display, 
                   barbell_lift, notes, rx_or_scaled, is_pr, source
            FROM workouts
            WHERE date >= ?
            ORDER BY date ASC, id ASC
        """, (start_date,))
        rows = cursor.fetchall()
    
    data = [dict(row) for row in rows]
    
    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2)
    
    if not data:
        return "No workouts found for this period."
    
    result = "## Workout History\n\n"
    result += "| Date | Workout | Result |\n"
    result += "|------|---------|--------|\n"
    for r in data:
        result += f"| {r['date']} | {r['title']} | {r['result_display'] or '-'} |\n"
    
    return result


class GetMobilityHistoryInput(BaseModel):
    """Input for getting mobility history."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    days_back: int = Field(default=30, description="Number of days to look back", ge=1, le=365)
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)


@mcp.tool(
    name="fitness_get_mobility_history",
    annotations={
        "title": "Get Mobility History",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def fitness_get_mobility_history(params: GetMobilityHistoryInput) -> str:
    """Get mobility work history.
    
    Returns mobility sessions over time.
    
    Args:
        params: GetMobilityHistoryInput with days_back
        
    Returns:
        str: Mobility data as JSON or markdown
    """
    start_date = (date.today() - timedelta(days=params.days_back)).isoformat()
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT date, duration_minutes, focus_area, exercises, notes
            FROM mobility_log
            WHERE date >= ?
            ORDER BY date ASC
        """, (start_date,))
        rows = cursor.fetchall()
    
    data = [dict(row) for row in rows]
    
    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2)
    
    if not data:
        return "No mobility data found for this period."
    
    result = "## Mobility History\n\n"
    result += "| Date | Duration | Focus | Exercises |\n"
    result += "|------|----------|-------|----------|\n"
    for r in data:
        exercises = (r['exercises'] or "")[:30]
        result += f"| {r['date']} | {r['duration_minutes']} min | {r['focus_area'] or '-'} | {exercises} |\n"
    
    return result


# ============================================================================
# Protein Estimation Helper
# ============================================================================

class EstimateProteinInput(BaseModel):
    """Input for estimating protein from food description."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    food_description: str = Field(..., description="Description of food eaten", min_length=1)


# Common protein estimates (grams per typical serving)
PROTEIN_ESTIMATES = {
    "chicken breast": 30,
    "chicken thigh": 25,
    "chicken": 25,
    "steak": 30,
    "beef": 25,
    "ground beef": 22,
    "salmon": 25,
    "fish": 22,
    "tuna": 25,
    "shrimp": 20,
    "eggs": 6,  # per egg
    "egg": 6,
    "greek yogurt": 15,
    "yogurt": 10,
    "cottage cheese": 14,
    "milk": 8,
    "cheese": 7,
    "protein shake": 25,
    "protein bar": 20,
    "tofu": 15,
    "beans": 15,
    "lentils": 18,
    "chickpeas": 15,
    "peanut butter": 8,
    "almonds": 6,
    "nuts": 5,
    "rice": 4,
    "bread": 3,
    "pasta": 7,
    "quinoa": 8,
}


@mcp.tool(
    name="fitness_estimate_protein",
    annotations={
        "title": "Estimate Protein Content",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def fitness_estimate_protein(params: EstimateProteinInput) -> str:
    """Estimate protein content from a food description.
    
    Provides rough protein estimates based on common foods. Use this to help
    log protein when you tell me what you ate.
    
    Args:
        params: EstimateProteinInput with food description
        
    Returns:
        str: Estimated protein with breakdown
    """
    description = params.food_description.lower()
    
    total = 0
    found_items = []
    
    for food, protein in PROTEIN_ESTIMATES.items():
        if food in description:
            # Check for quantities
            quantity = 1
            
            # Look for numbers before the food item
            pattern = rf'(\d+)\s*(?:oz|ounce|piece|slice|cup|scoop|serving)?s?\s*(?:of\s+)?{food}'
            match = re.search(pattern, description)
            if match:
                quantity = int(match.group(1))
                # Adjust for oz if applicable (assume standard serving is ~4oz for meats)
                if 'oz' in description or 'ounce' in description:
                    quantity = quantity / 4
            
            item_protein = int(protein * quantity)
            total += item_protein
            found_items.append(f"{food}: ~{item_protein}g")
    
    if not found_items:
        return f"I couldn't identify specific foods in '{params.food_description}'. Can you be more specific about the protein sources (e.g., chicken, eggs, greek yogurt)?"
    
    result = f"## Protein Estimate\n\n"
    result += f"**{params.food_description}**\n\n"
    
    for item in found_items:
        result += f"- {item}\n"
    
    result += f"\n**Estimated total: ~{total}g protein**\n\n"
    result += "*Note: These are rough estimates. Actual amounts vary by portion size.*\n\n"
    result += f"Want me to log this? Use `fitness_add_protein` with grams={total}"
    
    return result


# ============================================================================
# Google Calendar Integration
# ============================================================================

SCOPES = ['https://www.googleapis.com/auth/calendar']
CREDENTIALS_PATH = os.environ.get(
    "GOOGLE_CREDENTIALS_PATH",
    os.path.expanduser("~/fitness_mcp/credentials.json"),
)
TOKEN_PATH = os.environ.get(
    "GOOGLE_TOKEN_PATH",
    os.path.expanduser("~/fitness_mcp/token.json"),
)


def get_calendar_service():
    """Get authenticated Google Calendar service."""
    if not GOOGLE_CALENDAR_AVAILABLE:
        return None
    
    creds = None
    
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                return None
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        
        with open(TOKEN_PATH, 'w') as token:
            token.write(creds.to_json())
    
    return build('calendar', 'v3', credentials=creds)


def format_workout_for_calendar(day_workout: dict, week_num: int, week_theme: str) -> str:
    """Format workout details for calendar event description."""
    description = f"Week {week_num}: {week_theme}\n\n"
    
    if day_workout.get('exercises'):
        description += "EXERCISES:\n"
        for ex in day_workout['exercises']:
            reps = ex.get('reps', '')
            line = f"• {ex['name']}: {ex['sets']} x {reps}"
            if ex.get('notes'):
                line += f" - {ex['notes']}"
            description += line + "\n"
    
    if day_workout.get('conditioning'):
        description += f"\nCONDITIONING:\n{day_workout['conditioning']}\n"
    
    if day_workout.get('mobility'):
        description += f"\nMOBILITY:\n{day_workout['mobility']}\n"
    
    description += "\n---\nLogged via Fitness MCP"
    
    return description


class SyncCalendarInput(BaseModel):
    """Input for syncing program to calendar."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    start_date: Optional[str] = Field(default=None, description="Start date (YYYY-MM-DD), defaults to program start")
    calendar_id: str = Field(default="primary", description="Calendar ID to sync to")
    attendees: Optional[List[str]] = Field(default=None, description="List of email addresses to invite")


@mcp.tool(
    name="fitness_sync_calendar",
    annotations={
        "title": "Sync Program to Calendar",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True
    }
)
async def fitness_sync_calendar(params: SyncCalendarInput) -> str:
    """Sync the current training program to Google Calendar.
    
    Creates calendar events for each workout day in the program.
    Events are scheduled weekdays 10am-12pm.
    
    Args:
        params: SyncCalendarInput with optional start date
        
    Returns:
        str: Summary of created events
    """
    if not GOOGLE_CALENDAR_AVAILABLE:
        return "Error: Google Calendar libraries not installed. Run: pip install google-auth-oauthlib google-api-python-client"
    
    service = get_calendar_service()
    if not service:
        return f"Error: Google Calendar not configured. Place credentials.json in {CREDENTIALS_PATH}"
    
    # Get active program
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM programs WHERE is_active = TRUE LIMIT 1")
        program = cursor.fetchone()
    
    if not program:
        return "Error: No active program. Use fitness_set_program first."
    
    program_data = json.loads(program['program_data'])
    start_date = params.start_date or program['start_date']
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    
    events_created = 0
    
    # Iterate through 4 weeks
    for week_idx, week_data in enumerate(program_data['weeks']):
        week_num = week_idx + 1
        week_theme = week_data.get('theme', f'Week {week_num}')
        
        # Get the days structure (some weeks reference week 1)
        if isinstance(week_data.get('days'), dict):
            days = week_data['days']
        else:
            days = program_data['weeks'][0]['days']
        
        # Schedule weekdays only
        for day_offset in range(7):
            current_date = start_dt + timedelta(weeks=week_idx, days=day_offset)
            day_name = current_date.strftime("%A")
            
            # Skip weekends
            if day_name in ["Saturday", "Sunday"]:
                continue
            
            day_workout = days.get(day_name)
            if not day_workout or day_workout['name'] in ["Rest", "Rest or Light Mobility"]:
                continue
            
            # Create event 10am-12pm
            event_start = current_date.replace(hour=10, minute=0)
            event_end = current_date.replace(hour=12, minute=0)
            
            event = {
                'summary': f"💪 {day_workout['name']}",
                'description': format_workout_for_calendar(day_workout, week_num, week_theme),
                'start': {
                    'dateTime': event_start.isoformat(),
                    'timeZone': 'America/New_York',
                },
                'end': {
                    'dateTime': event_end.isoformat(),
                    'timeZone': 'America/New_York',
                },
                'reminders': {
                    'useDefault': False,
                    'overrides': [
                        {'method': 'popup', 'minutes': 60},
                    ],
                },
            }
            
            if params.attendees:
                event['attendees'] = [{'email': email} for email in params.attendees]
            
            try:
                service.events().insert(calendarId=params.calendar_id, body=event).execute()
                events_created += 1
            except Exception as e:
                return f"Error creating event: {str(e)}"
    
    return f"✅ Synced program to Google Calendar!\n\n- **Events created:** {events_created}\n- **Start date:** {start_date}\n- **Schedule:** Weekdays 10am-12pm"


class CreateCalendarEventInput(BaseModel):
    """Input for creating a single calendar event."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    title: str = Field(..., description="Event title", min_length=1)
    date: str = Field(..., description="Event date (YYYY-MM-DD)")
    start_time: str = Field(default="10:00", description="Start time (HH:MM)")
    end_time: str = Field(default="12:00", description="End time (HH:MM)")
    description: Optional[str] = Field(default=None, description="Event description")
    calendar_id: str = Field(default="primary", description="Calendar ID")
    attendees: Optional[List[str]] = Field(default=None, description="List of email addresses to invite")


@mcp.tool(
    name="fitness_create_calendar_event",
    annotations={
        "title": "Create Calendar Event",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True
    }
)
async def fitness_create_calendar_event(params: CreateCalendarEventInput) -> str:
    """Create a single workout event in Google Calendar.
    
    Args:
        params: CreateCalendarEventInput with event details
        
    Returns:
        str: Confirmation of created event
    """
    if not GOOGLE_CALENDAR_AVAILABLE:
        return "Error: Google Calendar libraries not installed. Run: pip install google-auth-oauthlib google-api-python-client"
    
    service = get_calendar_service()
    if not service:
        return f"Error: Google Calendar not configured. Place credentials.json in {CREDENTIALS_PATH}"
    
    event_date = datetime.strptime(params.date, "%Y-%m-%d")
    start_hour, start_min = map(int, params.start_time.split(":"))
    end_hour, end_min = map(int, params.end_time.split(":"))
    
    event_start = event_date.replace(hour=start_hour, minute=start_min)
    event_end = event_date.replace(hour=end_hour, minute=end_min)
    
    event = {
        'summary': params.title,
        'description': params.description or "",
        'start': {
            'dateTime': event_start.isoformat(),
            'timeZone': 'America/New_York',
        },
        'end': {
            'dateTime': event_end.isoformat(),
            'timeZone': 'America/New_York',
        },
    }
    
    if params.attendees:
        event['attendees'] = [{'email': email} for email in params.attendees]
    
    try:
        created_event = service.events().insert(calendarId=params.calendar_id, body=event).execute()
        return f"✅ Created event: **{params.title}** on {params.date} ({params.start_time}-{params.end_time})"
    except Exception as e:
        return f"Error creating event: {str(e)}"


@mcp.tool(
    name="fitness_check_calendar_setup",
    annotations={
        "title": "Check Calendar Setup",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def fitness_check_calendar_setup() -> str:
    """Check if Google Calendar integration is configured.
    
    Returns:
        str: Status of calendar setup
    """
    if not GOOGLE_CALENDAR_AVAILABLE:
        return "❌ Google Calendar libraries not installed.\n\nRun: `pip install google-auth-oauthlib google-api-python-client`"
    
    if not os.path.exists(CREDENTIALS_PATH):
        return f"❌ credentials.json not found.\n\nPlace your Google OAuth credentials at:\n`{CREDENTIALS_PATH}`\n\nYou can copy this from your friendship-mcp folder."
    
    service = get_calendar_service()
    if service:
        return "✅ Google Calendar is configured and ready!"
    else:
        return "❌ Could not authenticate with Google Calendar. Check your credentials."


# ============================================================================
# Main Entry Point
# ============================================================================

if __name__ == "__main__":
    mcp.run()
