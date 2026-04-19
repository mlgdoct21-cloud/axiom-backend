"""Waitlist API router - for landing page newsletter signups"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from core.database import get_db
from models.waitlist import Waitlist
from schemas.waitlist_schema import WaitlistSignup, WaitlistResponse, WaitlistMessage

router = APIRouter(prefix="/waitlist", tags=["waitlist"])


@router.post("/signup", response_model=WaitlistMessage, status_code=201)
async def signup_waitlist(
    signup: WaitlistSignup,
    db: Session = Depends(get_db)
):
    """
    Sign up for the waitlist (landing page)

    - **email**: Email address (required)
    - **name**: Full name (optional)
    - **source**: Where signup came from (default: landing_page)
    """
    try:
        # Check if email already exists
        existing = db.query(Waitlist).filter(Waitlist.email == signup.email).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered for waitlist"
            )

        # Create new waitlist entry
        waitlist_entry = Waitlist(
            email=signup.email,
            name=signup.name,
            source=signup.source
        )

        db.add(waitlist_entry)
        db.commit()
        db.refresh(waitlist_entry)

        return WaitlistMessage(
            message="Successfully added to waitlist! Check your email soon.",
            email=signup.email
        )

    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered for waitlist"
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error signing up: {str(e)}"
        )


@router.get("/count", response_model=dict)
async def get_waitlist_count(db: Session = Depends(get_db)):
    """Get total waitlist count (public endpoint)"""
    count = db.query(Waitlist).count()
    return {"total": count}


@router.get("/list", response_model=list[WaitlistResponse])
async def get_waitlist(db: Session = Depends(get_db)):
    """
    Get all waitlist entries (admin endpoint)
    TODO: Add authentication/admin check
    """
    entries = db.query(Waitlist).order_by(Waitlist.created_at.desc()).all()
    return entries
