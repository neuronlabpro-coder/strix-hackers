import asyncio
import enum
import hashlib
from pathlib import Path
import sys

# 1. Asegurar resolución de rutas desde la raíz
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
  sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import select
from backend.core.database import AsyncSessionLocal
import backend.core.security as sec
import backend.apps.organizations.models as org_models
from backend.apps.organizations.models import (
    User,
    Organization,
    Membership,
    PlanTierEnum,
)

# 2. Localizar la función de hash disponible en el backend
pwd_hasher = None
for candidate in ["hash_password", "get_password_hash", "hash_secret"]:
  if hasattr(sec, candidate):
    pwd_hasher = getattr(sec, candidate)
    break

if not pwd_hasher:
  for name in dir(sec):
    if "hash" in name.lower() and "verify" not in name.lower():
      fn = getattr(sec, name)
      if callable(fn):
        pwd_hasher = fn
        break

if not pwd_hasher:
  import bcrypt

  def fallback_hasher(pwd: str) -> str:
    sha = hashlib.sha256(pwd.encode("utf-8")).hexdigest()
    return bcrypt.hashpw(sha.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode(
        "utf-8"
    )

  pwd_hasher = fallback_hasher

# 3. Localizar el valor del rol de Membership (Enum o string)
admin_role = "admin"
for attr_name in dir(org_models):
  attr = getattr(org_models, attr_name)
  if (
      isinstance(attr, type)
      and issubclass(attr, enum.Enum)
      and "role" in attr_name.lower()
  ):
    for member in attr:
      if member.name.lower() == "admin" or str(member.value).lower() == "admin":
        admin_role = member
        break
    break

# 4. Definición de usuarios de prueba
TEST_ACCOUNTS = [
    {
        "email": "user1@mindguard.tech",
        "password": "UserPass2026!",
        "full_name": "Standard User (Free Tier)",
        "org_name": "Alpha Startup",
        "org_slug": "alpha-startup",
        "plan_tier": PlanTierEnum.FREE,
        "credits": 25.0,
    },
    {
        "email": "enterprise1@mindguard.tech",
        "password": "EnterprisePass2026!",
        "full_name": "Security Officer (Enterprise)",
        "org_name": "MegaCorp Enterprise",
        "org_slug": "megacorp-enterprise",
        "plan_tier": PlanTierEnum.ENTERPRISE,
        "credits": 5000.0,
    },
]


async def create_or_update_account(session, account_data):
  email = account_data["email"]
  password = account_data["password"]
  hashed = pwd_hasher(password)

  # 1. Gestionar usuario (No superuser, email ya verificado)
  res_user = await session.execute(select(User).where(User.email == email))
  user = res_user.scalars().first()

  if user:
    user.hashed_password = hashed
    user.full_name = account_data["full_name"]
    user.is_active = True
    user.is_superuser = False
    user.email_verified = True
  else:
    user = User(
        email=email,
        hashed_password=hashed,
        full_name=account_data["full_name"],
        is_active=True,
        is_superuser=False,
        email_verified=True,
    )
    session.add(user)

  await session.flush()

  # 2. Gestionar organización
  res_org = await session.execute(
      select(Organization).where(
          Organization.slug == account_data["org_slug"]
      )
  )
  org = res_org.scalars().first()

  if not org:
    org = Organization(
        name=account_data["org_name"],
        slug=account_data["org_slug"],
        plan_tier=account_data["plan_tier"],
        credit_balance=account_data["credits"],
    )
    session.add(org)
    await session.flush()
  else:
    org.plan_tier = account_data["plan_tier"]
    org.credit_balance = account_data["credits"]

  # 3. Gestionar membresía
  res_mem = await session.execute(
      select(Membership).where(
          Membership.organization_id == org.id, Membership.user_id == user.id
      )
  )
  membership = res_mem.scalars().first()

  if not membership:
    membership = Membership(
        organization_id=org.id,
        user_id=user.id,
        role=admin_role,
        is_active=True,
    )
    session.add(membership)
  else:
    membership.role = admin_role
    membership.is_active = True


async def main():
  print("[*] Conectando a PostgreSQL para aprovisionar usuarios de prueba...")
  async with AsyncSessionLocal() as session:
    for account in TEST_ACCOUNTS:
      await create_or_update_account(session, account)
    await session.commit()

  print("\n" + "=" * 65)
  print(" [OK] Cuentas de prueba aprovisionadas con éxito:")
  print("=" * 65)
  for acc in TEST_ACCOUNTS:
    print(f"\n* Tipo: {acc['plan_tier'].value.upper()}")
    print(f"  Email:        {acc['email']}")
    print(f"  Password:     {acc['password']}")
    print(f"  Workspace:    {acc['org_name']} (Créditos: {acc['credits']})")
    print(f"  SuperUser:    False (Aislamiento de cliente estricto)")
  print("=" * 65 + "\n")


if __name__ == "__main__":
  asyncio.run(main())