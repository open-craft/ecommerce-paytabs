"""PayTabs response processing views."""
import logging

import requests
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View
from oscar.apps.partner import strategy
from oscar.core.loading import get_class, get_model

from ecommerce.extensions.checkout.mixins import EdxOrderPlacementMixin
from ecommerce.extensions.checkout.utils import get_receipt_page_url

from .processors import PayTabs

logger = logging.getLogger(__name__)

Applicator = get_class('offer.applicator', 'Applicator')
Basket = get_model('basket', 'Basket')
OrderNumberGenerator = get_class('order.utils', 'OrderNumberGenerator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
NoShippingRequired = get_class('shipping.methods', 'NoShippingRequired')

PAYTABS_SUCCESS_CODES = ['100', '111']


class PayTabsResponseView(EdxOrderPlacementMixin, View):
    """
    View to handle the response from PayTabs after processing the payment.
    """
    @property
    def payment_processor(self):
        return PayTabs(self.request.site)

    @method_decorator(transaction.non_atomic_requests)
    @method_decorator(csrf_exempt)
    def dispatch(self, request, *args, **kwargs):
        return super(PayTabsResponseView, self).dispatch(request, *args, **kwargs)

    def _verify_response(self, request, payment_reference):
        """
        Verify the given payment_reference number to confirm that it is for a valid transaction
        and return the verification response data.
        """
        partner_short_code = request.site.siteconfiguration.partner.short_code
        configuration = settings.PAYMENT_PROCESSOR_CONFIG[partner_short_code.lower()][self.payment_processor.NAME]
        api_parameters = {
            "merchant_email": configuration['merchant_email'],
            "secret_key": configuration['secret_key'],
            "payment_reference": payment_reference
        }
        response = requests.post("https://www.paytabs.com/apiv2/verify_payment", data=api_parameters)
        return response.json()

    def _get_basket(self, basket_id):
        """
        Return the basket for the given id or None.
        """
        if not basket_id:
            return None

        try:
            basket_id = int(basket_id)
            basket = Basket.objects.get(id=basket_id)
            basket.strategy = strategy.Default()
            Applicator().apply(basket, basket.owner, self.request)
            return basket
        except (ValueError, ObjectDoesNotExist):
            return None

    def post(self, request):
        """
        Handle the POST request from PayTabs and redirect to the appropriate page based on the status.
        """
        transaction_id = 'Unknown'
        basket = None
        verification_data = {}
        try:
            payment_reference = request.POST.get('payment_reference')
            if payment_reference is None:
                logger.error('Received an invalid PayTabs merchant notification [%s]', request.POST)
                return redirect(reverse('payment_error'))

            logger.info('Received PayTabs merchant notification with payment_reference %s', payment_reference)
            verification_data = self._verify_response(request, payment_reference)
            if not verification_data['response_code'] in PAYTABS_SUCCESS_CODES:
                logger.error(
                    'Received an error (%i) from PayTabs merchant notification [%s]',
                    verification_data['response_code'],
                    request.POST
                )
                return redirect(reverse('payment_error'))
            reference_number = verification_data['reference_no']
            basket_id = OrderNumberGenerator().basket_id(reference_number)
            basket = self._get_basket(basket_id)
            transaction_id = verification_data['transaction_id']
            if not basket:
                logger.error('Received payment for non-existent basket [%s].', basket_id)
                return redirect(reverse('payment_error'))
        finally:
            payment_processor_response = self.payment_processor.record_processor_response(
                request.POST, transaction_id=transaction_id, basket=basket
            )

        try:
            with transaction.atomic():
                try:
                    self.handle_payment(verification_data, basket)
                except Exception as exc:
                    logger.exception(
                        'PayTabs payment did not complete for basket [%d] because of [%s]. '
                        'The payment response was recorded in entry [%d].',
                        basket.id,
                        exc.__class__.__name__,
                        payment_processor_response.id
                    )
                    raise
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception(
                'Attempts to handle payment for basket [%d] failed due to [%s].',
                basket.id,
                exc.__class__.__name__
            )
            return redirect(reverse('payment_error'))
        self.create_order(request, basket)
        receipt_url = get_receipt_page_url(
            order_number=basket.order_number,
            site_configuration=basket.site.siteconfiguration
        )
        return redirect(receipt_url)
