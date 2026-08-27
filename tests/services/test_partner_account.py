from app.config import settings
from app.database.models import User
from app.utils.user_utils import (
    get_effective_referral_commission_percent,
    is_partner_account,
    is_rays_program_available_for,
    is_rays_shop_available_for,
)


def _user(**kwargs) -> User:
    user = User()
    user.is_partner = kwargs.pop("is_partner", False)
    user.referral_commission_percent = kwargs.pop("referral_commission_percent", None)
    return user


def test_partner_earns_the_raised_commission():
    assert get_effective_referral_commission_percent(_user()) == settings.REFERRAL_COMMISSION_PERCENT
    assert (
        get_effective_referral_commission_percent(_user(is_partner=True))
        == settings.PARTNER_REFERRAL_COMMISSION_PERCENT
    )


def test_an_explicit_percent_outranks_the_partner_flag():
    """Иначе админ не смог бы отклониться от партнёрских 60%."""
    user = _user(is_partner=True, referral_commission_percent=75)
    assert get_effective_referral_commission_percent(user) == 75


def test_partner_is_outside_the_rays_program():
    partner = _user(is_partner=True)
    assert is_partner_account(partner)
    assert is_rays_program_available_for(partner) is False
    assert is_rays_shop_available_for(partner) is False


def test_ordinary_user_follows_the_global_rays_switch():
    ordinary = _user()
    assert is_rays_program_available_for(ordinary) is settings.is_rays_program_enabled()
    assert is_rays_shop_available_for(ordinary) is settings.is_rays_shop_enabled()


def test_a_user_without_the_column_loaded_is_not_a_partner():
    """getattr-защита: объекты без is_partner не должны падать."""

    class Bare:
        referral_commission_percent = None

    assert is_partner_account(Bare()) is False
    assert get_effective_referral_commission_percent(Bare()) == settings.REFERRAL_COMMISSION_PERCENT
