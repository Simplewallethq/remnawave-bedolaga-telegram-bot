# Apple Root CA certificates

DER-encoded (`.cer`) Apple root certificates, downloaded from the official Apple
certificate authority page: https://www.apple.com/certificateauthority/

Used by `app.services.apple_iap_service` to build the trust anchor list passed to
`appstoreserverlibrary.signed_data_verifier.SignedDataVerifier(root_certificates=...)`
when verifying StoreKit 2 signed transactions (JWS). StoreKit 2 leaf certificates chain
up to `AppleRootCA-G3.cer`; the other roots are included for completeness and are
harmless no-ops during chain verification if unused.

To refresh: re-download the `.cer` files from the same page and replace them here.
