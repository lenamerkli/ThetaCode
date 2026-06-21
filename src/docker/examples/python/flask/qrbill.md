From 2020, Swiss payment slips will progressively be converted to the QR-bill format. Specifications can be found on https://www.paymentstandards.ch/
This library is aimed to produce properly-formatted QR-bills as SVG files either from command line input or by using the QRBill class.
```bash
./.venv/bin/pip install qrbill
```
```python
@app.route('/api/v1/qrbill/<product>', methods=['GET'])
@login_required
def r_api_v1_qrbill(product: str):
    if (product not in PRICES.keys()) or (product not in PRODUCTS.keys()):
        return {
            'error': 'not found',
            'message': 'The product was not found.',
        }, 404
    account = Login.load(session['account']).get_account()
    reference_number = randint(10**10, 10**11 - 1)
    bill = qrbill.QRBill(
        account=environ['BILLING_ACCOUNT'].replace(' ', ''),
        creditor={
            'name': environ['BILLING_NAME'],
            'pcode': environ['BILLING_POSTCODE'],
            'city': environ['BILLING_CITY'],
            'country': 'CH',
        },
        amount=PRICES[product],
        additional_information=f"{PRODUCTS[product]} für {account.mail}",
        reference_number=reference_number,
        language='de',
    )
    bill.as_svg(relative_path(f"temp/qrbills/{reference_number}.svg"))
    resp = make_response(send_from_directory(relative_path('temp/qrbills'), f"{reference_number}.svg"))
    resp.mimetype = 'image/svg+xml'
    return resp
```
