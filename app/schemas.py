import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

Provider = Literal["mtn", "airtel"]
PayoutStatus = Literal["pending", "paid", "rejected", "cancelled"]


def normalise_phone(v: str) -> str:
    """07XXXXXXXX, 2567XXXXXXXX or +2567XXXXXXXX -> +256XXXXXXXXX (Ugandan mobile numbers)."""
    d = re.sub(r"[\s-]", "", v)
    if re.fullmatch(r"\+2567\d{8}", d):
        return d
    if re.fullmatch(r"2567\d{8}", d):
        return "+" + d
    if re.fullmatch(r"07\d{8}", d):
        return "+256" + d[1:]
    raise ValueError("Enter a valid Ugandan mobile number, like 0772 123456 or 0701 234567.")


class RegisterIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

    @field_validator("full_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Enter your name.")
        return v


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


class RefreshIn(BaseModel):
    refresh_token: str = Field(max_length=512)


class PasswordResetRequestIn(BaseModel):
    email: EmailStr


class PasswordResetConfirmIn(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=8, max_length=128)


class MessageOut(BaseModel):
    message: str


class TokensOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: str


class BalanceOut(BaseModel):
    total_earned_ugx: int
    available_ugx: int
    pending_ugx: int
    paid_out_ugx: int
    validated_count: int
    min_cashout_ugx: int


class WorkOut(BaseModel):
    id: int
    noisy_english: str
    attempted: str
    difficulty: Literal["easy", "medium", "hard"]
    reward_ugx: int


class DatasetProgressOut(BaseModel):
    total_sentences: int
    finished_sentences: int
    completion_percentage: float
    max_budget_ugx: int | None = None
    spent_budget_ugx: int = 0
    remaining_budget_ugx: int | None = None
    is_budget_exhausted: bool = False


class FinishedSentenceOut(BaseModel):
    id: int
    noisy_english: str
    attempted: str
    clean_lug: str
    validated_at: datetime


class SubmitIn(BaseModel):
    clean_lug: str = Field(max_length=2000)
    confirmed_unchanged: bool = False

    @field_validator("clean_lug")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("The translation can't be empty.")
        return v


class SubmitOut(BaseModel):
    reward_ugx: int


class MethodIn(BaseModel):
    provider: Provider
    phone_number: str
    account_name: str = Field(min_length=2, max_length=100)

    @field_validator("phone_number")
    @classmethod
    def _phone(cls, v: str) -> str:
        return normalise_phone(v)

    @field_validator("account_name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) < 2:
            raise ValueError("Enter the name registered on that number.")
        return v


class MethodOut(BaseModel):
    id: uuid.UUID
    provider: Provider
    phone_number: str
    account_name: str
    is_default: bool


class PayoutIn(BaseModel):
    payment_method_id: uuid.UUID


class PayoutOut(BaseModel):
    id: uuid.UUID
    amount_ugx: int
    status: PayoutStatus
    requested_at: datetime
    provider: Provider
    phone_number: str
    account_name: str


class AdminPayoutOut(PayoutOut):
    user_id: uuid.UUID
    user_name: str
    user_email: str
    sentence_count: int
    provider_reference: str | None = None
    rejection_reason: str | None = None
    processed_at: datetime | None = None


class MarkPaidIn(BaseModel):
    provider_reference: str = Field(min_length=3, max_length=100)


class RejectIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
