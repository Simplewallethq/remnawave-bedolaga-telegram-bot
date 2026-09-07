from app.localization.branding import apply_brand


def test_brand_replaces_the_app_name_in_every_letter_case():
    assert apply_brand("Скачать Leto VPN", "Shuka") == "Скачать Shuka VPN"
    assert apply_brand("LETO VPN", "Shuka") == "SHUKA VPN"
    assert apply_brand("leto vpn", "Shuka") == "shuka vpn"


def test_brand_leaves_domains_and_identifiers_alone():
    # Renaming any of these breaks a link, a bundle id or a bot handle rather
    # than rebranding a sentence.
    untouched = [
        "https://letovpn.com/tv",
        "https://m-app.letovpn.com/internal/v1/tv/pairing/telegram",
        "com.letovpn.ios.subscription.monthly",
        "googleplay.review@letovpn.com",
        "@letovpn_bot",
    ]
    for value in untouched:
        assert apply_brand(value, "Shuka") == value


def test_brand_rewrites_prose_but_not_the_link_inside_it():
    source = 'Скачай Leto на <a href="https://letovpn.com">letovpn.com</a>'
    assert apply_brand(source, "Shuka") == (
        'Скачай Shuka на <a href="https://letovpn.com">letovpn.com</a>'
    )


def test_empty_brand_or_text_is_a_no_op():
    assert apply_brand("Leto VPN", "") == "Leto VPN"
    assert apply_brand("", "Shuka") == ""
