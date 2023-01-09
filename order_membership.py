"""
    membership order
"""
@membership.route('/order/membership', methods=['POST'])
@login_required
@logevent_request
@exception_handler
def order_membership():
    params = g.params
    now = int(time.time())
    params = g.params
    schema = {
        'membership_type': {'type': 'string', 'empty': False},
        'billing_address_id': {'type': 'string', 'empty': False},
        'shipping_address_id': {'type': 'string', 'empty': False},
        'last_four': {'type': 'string', 'nullable': True},
        'stripe_token': {'type': 'string', 'nullable': True},
        'withFree30Days': {'type':'boolean', 'nullable': True},
        'affirm_token': {'type': 'string', 'nullable': True},
        'coupon': {'type': 'string', 'nullable': True},
        'type': {'type': 'string', 'nullable': True},
        'is_test':{'type': 'boolean', 'nullable': True, 'default':False, 'required': False}
    }
    current_app.logger.info('POST /order/membership: {}'.format(params))
    membership_type = int(params.get('membership_type'))

    print("@>01: /order/membership/ - params:{}".format(params))

    v = Validator(schema)
    v.validate(params)
    if v.errors:
        return json_wrapper(code=501, data=v.errors)

    # get user info
    token = get_token_from_header(request)
    user_id = int(redisGet(token))
    if not user_id:
        raise Exception('Invalid User.')

    couponCode = params.get('coupon')
    withFree30Days = params.get('withFree30Days')
    isTest = params.get('is_test')

    couponExtraMonths = 0
    if couponCode:
        couponCode = couponCode.upper()
        coupon = MembershipCoupon.query.filter(MembershipCoupon.code == couponCode, MembershipCoupon.status == MembershipCouponStatus.VALID).first()
        if not coupon:
            with sentry_sdk.push_scope() as scope:
                scope.set_tag("userId", user_id)
                scope.set_extra("code", couponCode)
                sentry_sdk.capture_message("membership coupon: invalid coupon")
            raise Exception('Invalid coupon code (ERR1)')
        else:
            print("Coupon found:{}".format(coupon))
            now = int(time.time())
            print("Coupon now:{} start={} end={}".format(now, coupon.validity_start_time,coupon.validity_end_time ))
            if coupon.validity_start_time and now < coupon.validity_start_time:
                raise Exception('Invalid coupon code (ERR2)')
            if coupon.validity_end_time and now > coupon.validity_end_time:
                raise Exception('Invalid coupon code (ERR3)')
            # check if coupon available only for yearly plans
            if coupon.coupon_type == MembershipCouponType.FREE_MONTHS_WITH_YEARLY and membership_type != PlanType.YEARLY and membership_type != PlanType.DYEARLY:
                raise Exception('Invalid coupon code (ERR4)')

            usedRecord = MembershipCouponUsage.query.filter(MembershipCouponUsage.code == couponCode, MembershipCouponUsage.user_id == user_id).first()
            if usedRecord:
                with sentry_sdk.push_scope() as scope:
                    scope.set_tag("userId", user_id)
                    scope.set_extra("code", couponCode)
                    sentry_sdk.capture_message("membership coupon: already used")
                raise Exception('Coupon code already used')
            else:
                couponExtraMonths = coupon.free_months
                # we set to true so that we don't charge the user
                # for a monthly plan
                withFree30Days = True

    # check if we need to add an extra 30 days
    # if parameter is set to true, and we don't have activate a MONTLY
    # membership, then we add 30 days
    extra_duration = 2592000 if (withFree30Days and membership_type != PlanType.MONTHLY) else 0
    if couponExtraMonths != 0:
        extra_duration = couponExtraMonths * 2592000
    # for a Monthly plan and free 30days, we won't charge now
    dontChargeNow = withFree30Days and (membership_type == PlanType.MONTHLY)

    current_app.logger.info("@@ withFree30Days={}, extra_duration={}, dontChargeNow={}".format(withFree30Days, extra_duration, dontChargeNow))

    user = User.query.get(user_id)
    available_types = get_available_membership_options(user)
    current_app.logger.info("@@ user_id={}, available_types={}".format(user_id, available_types))
    if membership_type not in available_types:
        raise Exception('You are not eligible for this type of membership.')

    is_member = is_user_member(user)
    useAffirm = True if params.get('type') == 'affirm' else False


    # calculate total payment
    subtotal, duration, _, _, discount = calc_membership_price(membership_type)
    shipping = 0.0
    # apply extra duration (can be 0)
    duration += extra_duration

    # find geo locations
    billing_address_id = params.get('billing_address_id')
    ba = Address.query.filter(Address.uuid == params.get('billing_address_id')).first()
    if not ba:
        current_app.logger.info("@@ billing address not found uuid={}, user id={}".format(billing_address_id, user_id))
        raise Exception('Billing address not found.')

    shipping_address_id = params.get('shipping_address_id')
    if shipping_address_id:
        sa = Address.query.filter(Address.uuid == params.get('shipping_address_id')).first()
    else:
        sa = None

    # [DEV-246]: we need to consider tax associated to shipping address, not billing
    order_country = sa.country if sa else ba.country
    order_state = sa.state if sa else ba.state
    order_zip = sa.zip if sa else ba.zip
    order_address1 = sa.address1 if sa else ba.address1
    order_address2 = sa.address2 if sa else ba.address2
    order_city = sa.city if sa else ba.city
    order_geo_id = sa.geo_id if sa else ba.geo_id

    # calc tax
    try:
        tax_ob = tax_for_order(subtotal, order_country, order_state, order_zip, shipping)
    except Exception as e:
        current_app.logger.exception(e)
        return json_wrapper(code=502, data="Error from taxjar: {}".format(str(e)))

    tax = tax_ob.amount_to_collect

    # create order ob
    order = Order(
        uuid=create_order_num(type='membership'),
        user_id=user.id,
        name='{} {}'.format(ba.first_name, ba.last_name),
        email=user.email,
        mobile=user.mobile,
        address1=order_address1,
        address2=order_address2,
        city=order_city,
        state=order_state,
        zip=order_zip,
        country=order_country,
        geo_id=order_geo_id,
        plan_type=membership_type,
        payment_method=PaymentMethod.FINANCE if useAffirm else PaymentMethod.CREDIT,
        product_type=ProductType.MEMBERSHIP,
        total=0 if dontChargeNow else subtotal,
        shipping=shipping,
        tax=0 if dontChargeNow else tax,
        discount=discount
    )

    if billing_address_id:
        order.b_address1 = ba.address1
        order.b_address2 = ba.address2
        order.b_city = ba.city
        order.b_state = ba.state
        order.b_zip = ba.zip
        order.b_country = ba.country
        order.b_geo_id = ba.geo_id

    last_four = params.get('last_four')
    if last_four:
        if user.last_four and user.last_four != last_four:
            raise Exception('Invalid card info.')
        order.last_four = last_four
        user.last_four = last_four

    db.session.add(order)
    db.session.commit()

    current_app.logger.info("@@ useAffirm?={}".format(useAffirm))

    if useAffirm:
        affirmToken = params.get('affirm_token')
        if not affirmToken:
            current_app.logger.info("missing Affirm token!")
            raise Exception('Missing affirm token')
        a_order = authorize(affirmToken, order.id)
        current_app.logger.info("@@ affirm.authorize result={}".format(a_order))
        if not a_order.get('id'):
            raise Exception('Affirm authorize failure.')
        affirm_charge_id = a_order.get('id')
        current_app.logger.info("@@ affirm charge id={}".format(affirm_charge_id))
        a_capture = capture(affirm_charge_id, order.id)
        current_app.logger.info("@@ affirm.capture result={}".format(a_capture))
        if not a_capture.get('id'):
            raise Exception('Affirm capture failure.')
        order.affirm_charge_id = a_capture.get('id')
        db.session.commit()
    elif not isTest:
        # save stripe user info and charge if needs to charge
        stripe_token = params.get('stripe_token')
        stripe_customer = create_customer(stripe_token, user.email)
        # dontChargeNow = True

        if not dontChargeNow:
            try:
                stripe.api_key = STRIPE_PUB_KEY
                charge = stripe.Charge.create(
                    amount=int((subtotal + tax + shipping) * 100),
                    currency='usd',
                    description='Membership - {}'.format(order.uuid),
                    customer=stripe_customer.stripe_customer_id
                )
                if charge.get('id'):
                    order.stripe_customer_id = stripe_customer.stripe_customer_id
                    order.stripe_charge_id = charge.get('id')
                    order.payment_status = PaymentStatus.PAID
                    order.order_status = OrderStatus.PAID

                db.session.commit()
            except Exception as e:
                with sentry_sdk.push_scope() as scope:
                    scope.set_tag("userId", user_id)
                    scope.set_tag("what", "stripe_charge_failure")
                    scope.set_extra("stripe_token", stripe_token)
                    scope.set_extra("stripe_customer.stripe_customer_id", stripe_customer.stripe_customer_id)
                    scope.set_extra("message", "Failed to charge with stripe.(api/v1/membership.py - 473)")
                    sentry_sdk.capture_exception(e)

                raise Exception('Failed to charge with stripe.')

    membership = Membership(
        user_id=user.id,
        order_id=order.id,
        type=membership_type,
        duration=duration,
        status=MembershipStatus.UNUSED,
        uuid=str(uuid4())
    )

    # we record the fact that the user used the coupon
    if couponCode:
        usedRecord = MembershipCouponUsage(
            code=couponCode,
            user_id=user_id,
        )
        db.session.add(usedRecord)

    previousUserMembershipStatus=MembershipStatus.get_desc(user.membership_status)
    previousUserMembershipPlan=PlanType.get_desc(user.membership_type)

    print("\n@>01: BEFORE active_membership")
    print("@>01: user:{}".format(user.debug_info()))
    print("@>01: membership:{}".format(membership.debug_info()))
    print("\n##")

    user, membership, is_activated = active_membership_if_paired(user, membership)

    print("\n@>01: AFTER active_membership")
    print("@>01: user:{}".format(user.debug_info()))
    print("@>01: membership:{}".format(membership.debug_info()))
    print("\n##")

    # if the membership created is being activated we update the credits
    if membership.status == MembershipStatus.ACTIVE:
         extend_ycube_credits(user, membership_type)

    db.session.add(user)
    db.session.add(membership)
    db.session.commit()

    segment_track_user(user, "CreateMembershipBackend", 
        method='in-app',  
        before_pairing=(not is_activated),
        membership=membership.get_info(),
        order=order.get_info()
        )

    if is_activated:
        # we notify about the purchase for extra processing
        queue_membership_user_purchased_membership(user, membership, order)

        # we update segment user membership information
        segment_user_membership(user, previousUserMembershipStatus, previousUserMembershipPlan)


    # call helper function and update segment to find origin of membership purchase (e.g. amazon, walmart, etc.)
    membership_purchased_from = get_membership_purchased_from(order.uuid)
    if membership_purchased_from:
        segment_identify_user(user,
        membershipPurchasedFrom=membership_purchased_from)

    print("@@@ membership is: {}".format(membership))
    plan = Plan.query.filter(Plan.type == order.plan_type).first()
    total = round(float(order.total) + float(order.shipping) + float(order.tax), 2)
    subtotal = float(order.total)


    if not dontChargeNow:
        # save tax
        try:
            create_taxjar_order(order.uuid, order.total, order.tax, ba.country, ba.state, ba.zip, shipping)
        except Exception as e:
            with sentry_sdk.push_scope() as scope:
                scope.set_tag("orderUuid", order.uuid)
                scope.set_tag("userId", user_id)
                scope.set_extra("order.total", order.total)
                scope.set_extra("ba.country", ba.country)
                scope.set_extra("ba.state", ba.state)
                scope.set_extra("ba.zip", ba.zip)
                scope.set_extra("shipping", shipping)
                scope.set_extra("e", str(e))
                sentry_sdk.capture_exception(e)
            # we don't fail here - to finish the transaction
            # raise Exception('Failed to save tax in taxjar.')

    data = order.get_info()
    data['expiration_date'] = date.fromtimestamp(user.expiration_time).strftime('%m/%d/%Y') if user.expiration_time else None
    return json_wrapper(data=data)

