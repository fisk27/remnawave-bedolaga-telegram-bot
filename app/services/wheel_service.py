"""
Сервис колеса удачи (Fortune Wheel) с RTP алгоритмом.
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import get_subscription_by_user_id
from app.database.crud.user import add_user_balance
from app.database.crud.wheel import (
    get_or_create_wheel_config,
    get_user_spins_today,
    get_wheel_prizes,
    get_wheel_statistics,
)
from app.database.models import (
    PromoCode,
    PromoCodeType,
    Subscription,
    User,
    WheelConfig,
    WheelPrize,
    WheelPrizeType,
    WheelSpin,
    WheelSpinPaymentType,
)
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)

rng = secrets.SystemRandom()


@dataclass
class SpinResult:
    """Результат спина колеса."""

    success: bool
    prize_id: int | None = None
    prize_type: str | None = None
    prize_value: int = 0
    prize_display_name: str = ''
    emoji: str = '🎁'
    color: str = '#3B82F6'
    rotation_degrees: float = 0.0
    message: str = ''
    promocode: str | None = None
    error: str | None = None


@dataclass
class EligibleSubscription:
    """Подписка, доступная для оплаты колеса днями."""

    id: int
    tariff_name: str | None
    days_left: int


@dataclass
class SpinAvailability:
    """Доступность спина для пользователя."""

    can_spin: bool
    reason: str | None = None
    spins_remaining_today: int = 0
    can_pay_stars: bool = False
    can_pay_days: bool = False
    can_pay_tickets: bool = False
    min_subscription_days: int = 0
    user_subscription_days: int = 0
    user_balance_kopeks: int = 0
    required_balance_kopeks: int = 0
    eligible_subscriptions: list[EligibleSubscription] | None = None


class FortuneWheelService:
    """Сервис колеса удачи с RTP механикой."""

    def __init__(self):
        pass

    async def check_availability(self, db: AsyncSession, user: User) -> SpinAvailability:
        """Проверить доступность спина для пользователя."""
        config = await get_or_create_wheel_config(db)

        # Колесо выключено
        if not config.is_enabled:
            return SpinAvailability(
                can_spin=False,
                reason='wheel_disabled',
            )

        # Проверяем лимит спинов
        spins_today = await get_user_spins_today(db, user.id)
        spins_remaining = config.daily_spin_limit - spins_today if config.daily_spin_limit > 0 else 999

        if config.daily_spin_limit > 0 and spins_today >= config.daily_spin_limit:
            return SpinAvailability(
                can_spin=False,
                reason='daily_limit_reached',
                spins_remaining_today=0,
            )

        # Проверяем доступные способы оплаты
        can_pay_stars = False
        can_pay_days = False
        can_pay_tickets = (
            config.spin_cost_tickets_enabled
            and (user.spin_tickets or 0) >= (config.spin_cost_tickets or 1)
        )
        user_subscription_days = 0
        required_balance_kopeks = 0

        # Проверяем оплату Stars (конвертируется в рубли из баланса)
        if config.spin_cost_stars_enabled and config.spin_cost_stars > 0:
            stars_rate = Decimal(str(settings.get_stars_rate()))
            rubles = Decimal(config.spin_cost_stars) * stars_rate
            required_balance_kopeks = int(rubles * 100)
            # Проверяем достаточно ли средств на балансе
            if user.balance_kopeks >= required_balance_kopeks:
                can_pay_stars = True

        eligible_subs: list[EligibleSubscription] = []
        if config.spin_cost_days_enabled:
            if settings.is_multi_tariff_enabled():
                from app.database.crud.subscription import get_active_subscriptions_by_user_id

                active_subs = await get_active_subscriptions_by_user_id(db, user.id)
            else:
                _single = await get_subscription_by_user_id(db, user.id)
                active_subs = [_single] if _single else []

            min_days_required = config.min_subscription_days_for_day_payment + config.spin_cost_days
            for sub in active_subs:
                if not sub.is_active:
                    continue
                # Exclude daily tariffs — they can't pay with days
                is_daily = sub.tariff and getattr(sub.tariff, 'is_daily', False)
                if is_daily:
                    continue
                if sub.days_left >= min_days_required:
                    tariff_name = sub.tariff.name if sub.tariff else None
                    eligible_subs.append(
                        EligibleSubscription(id=sub.id, tariff_name=tariff_name, days_left=sub.days_left)
                    )

            if eligible_subs:
                can_pay_days = True
                # For backward compat: use best subscription's days
                user_subscription_days = max(s.days_left for s in eligible_subs)

        if not can_pay_stars and not can_pay_days and not can_pay_tickets:
            # Определяем причину
            reason = 'no_payment_method_available'
            if config.spin_cost_stars_enabled and user.balance_kopeks < required_balance_kopeks:
                reason = 'insufficient_balance'

            return SpinAvailability(
                can_spin=False,
                reason=reason,
                spins_remaining_today=spins_remaining,
                can_pay_stars=can_pay_stars,
                can_pay_days=can_pay_days,
                can_pay_tickets=can_pay_tickets,
                min_subscription_days=config.min_subscription_days_for_day_payment,
                user_subscription_days=user_subscription_days,
                user_balance_kopeks=user.balance_kopeks,
                required_balance_kopeks=required_balance_kopeks,
                eligible_subscriptions=eligible_subs or None,
            )

        # Проверяем наличие призов
        prizes = await get_wheel_prizes(db, config.id, active_only=True)
        if not prizes:
            return SpinAvailability(
                can_spin=False,
                reason='no_prizes_configured',
            )

        return SpinAvailability(
            can_spin=True,
            spins_remaining_today=spins_remaining,
            can_pay_stars=can_pay_stars,
            can_pay_days=can_pay_days,
            can_pay_tickets=can_pay_tickets,
            min_subscription_days=config.min_subscription_days_for_day_payment,
            user_subscription_days=user_subscription_days,
            user_balance_kopeks=user.balance_kopeks,
            required_balance_kopeks=required_balance_kopeks,
            eligible_subscriptions=eligible_subs or None,
        )

    def calculate_prize_probabilities(
        self, config: WheelConfig, prizes: list[WheelPrize], spin_cost_kopeks: int
    ) -> list[tuple[WheelPrize, float]]:
        """
        Рассчитать вероятности выпадения призов на основе RTP.

        Алгоритм:
        1. Целевая средняя выплата = spin_cost * (RTP / 100)
        2. Для призов с manual_probability - используем его напрямую
        3. Для остальных - рассчитываем веса обратно пропорционально стоимости приза
        4. "Nothing" сектор балансирует систему
        """
        if not prizes:
            return []

        target_payout = spin_cost_kopeks * (config.rtp_percent / 100)

        # Разделяем призы с ручной вероятностью и автоматической
        manual_prizes = []
        auto_prizes = []
        manual_prob_sum = 0.0

        for prize in prizes:
            if prize.manual_probability is not None and prize.manual_probability > 0:
                manual_prizes.append((prize, prize.manual_probability))
                manual_prob_sum += prize.manual_probability
            else:
                auto_prizes.append(prize)

        # Оставшаяся вероятность для авто-призов
        remaining_prob = max(0, 1.0 - manual_prob_sum)

        if not auto_prizes or remaining_prob <= 0:
            # Только ручные призы, нормализуем их
            if manual_prizes:
                total = sum(p[1] for p in manual_prizes)
                return [(p[0], p[1] / total) for p in manual_prizes]
            return []

        # Рассчитываем веса для авто-призов
        # Вес обратно пропорционален стоимости приза (более дорогие выпадают реже)
        weights = []
        for prize in auto_prizes:
            if prize.prize_value_kopeks > 0:
                # Чем дороже приз, тем меньше вес
                weight = target_payout / prize.prize_value_kopeks
            else:
                # "Nothing" или нулевой приз - даем базовый вес
                weight = 1.0
            weights.append((prize, max(weight, 0.01)))  # Минимальный вес 1%

        # Нормализуем веса авто-призов до remaining_prob
        total_weight = sum(w[1] for w in weights)
        auto_probabilities = [(prize, (weight / total_weight) * remaining_prob) for prize, weight in weights]

        # Объединяем
        result = manual_prizes + auto_probabilities

        # Финальная нормализация (на случай погрешностей)
        total = sum(p[1] for p in result)
        if total > 0:
            result = [(p[0], p[1] / total) for p in result]

        return result

    @staticmethod
    def _mask_username(user: User) -> str:
        """Маскировать имя пользователя для публичного отображения (например, 'Alexander' -> 'Al*****r')."""
        name = (user.username or user.first_name or '').strip()
        if len(name) <= 3:
            return name or 'User'
        return f'{name[:2]}*****{name[-1]}'

    async def _select_prize(
        self,
        db: AsyncSession,
        user: User,
        prizes_with_probabilities: list[tuple[WheelPrize, float]],
    ) -> WheelPrize | None:
        """Выбрать приз на основе вероятностей с учетом месячных лимитов и окон доступности.

        Returns ``None`` if no prizes remain after filtering by monthly limit / window.
        Caller handles this as a "nothing" result. monthly_wins_count is NOT incremented
        here — that happens atomically in spin() after the prize is successfully applied.
        """
        if not prizes_with_probabilities:
            raise ValueError('No prizes to select from')

        now = datetime.now(UTC)
        current_period = now.year * 100 + now.month  # e.g. 202605
        current_day = now.day

        # a) Сбрасываем месячные счетчики, если сменился месяц/год
        needs_flush = False
        for prize, _ in prizes_with_probabilities:
            if prize.monthly_limit is not None and prize.last_reset_month != current_period:
                prize.monthly_wins_count = 0
                prize.current_month_winner = None
                prize.last_reset_month = current_period
                needs_flush = True
        if needs_flush:
            await db.flush()

        # b) Отфильтровываем недоступные призы (лимит исчерпан / вне окна доступности)
        candidates: list[tuple[WheelPrize, float]] = []
        for prize, prob in prizes_with_probabilities:
            if prize.monthly_limit is not None and prize.monthly_wins_count >= prize.monthly_limit:
                continue
            if prize.window_start_day is not None and current_day < prize.window_start_day:
                continue
            if prize.window_end_day is not None and current_day > prize.window_end_day:
                continue
            candidates.append((prize, prob))

        if not candidates:
            return None

        # c) Гарантированный дроп в последний день окна: если есть невыданные
        #    призы с monthly_limit, у которых сегодня — последний день окна,
        #    один из них выпадает гарантированно (минуя взвешенный random).
        forced_candidates = [
            prize
            for prize, _ in candidates
            if (
                prize.window_end_day is not None
                and prize.window_end_day == current_day
                and prize.monthly_limit is not None
                and prize.monthly_wins_count < prize.monthly_limit
            )
        ]

        if forced_candidates:
            selected: WheelPrize = rng.choice(forced_candidates)
        else:
            # Нормализуем вероятности оставшихся кандидатов после фильтрации
            total = sum(p for _, p in candidates)
            normalized = (
                [(prize, prob / total) for prize, prob in candidates]
                if total > 0
                else list(candidates)
            )

            rand = rng.random()
            cumulative = 0.0
            selected = normalized[-1][0]
            for prize, probability in normalized:
                cumulative += probability
                if rand <= cumulative:
                    selected = prize
                    break

        return selected

    def _calculate_rotation(self, prizes: list[WheelPrize], selected_prize: WheelPrize) -> float:
        """
        Рассчитать угол поворота колеса для анимации.
        Возвращает градусы для CSS transform.
        """
        if not prizes:
            return 0.0

        # Находим индекс выбранного приза
        prize_index = next((i for i, p in enumerate(prizes) if p.id == selected_prize.id), 0)

        # Угол одного сектора
        sector_angle = 360 / len(prizes)

        # Базовый угол до центра сектора (от 12 часов по часовой)
        base_angle = prize_index * sector_angle + sector_angle / 2

        # Добавляем случайное смещение внутри сектора (не по краям)
        offset = rng.uniform(-sector_angle * 0.3, sector_angle * 0.3)

        # Угол остановки (стрелка сверху, поэтому инвертируем)
        stop_angle = 360 - base_angle + offset

        # Добавляем несколько полных оборотов для эффекта
        full_rotations = rng.randint(5, 8) * 360

        return full_rotations + stop_angle

    async def _process_stars_payment(self, db: AsyncSession, user: User, config: WheelConfig) -> int:
        """
        Обработать оплату Stars (списание эквивалента с баланса).
        Возвращает стоимость в копейках.
        """
        # Конвертируем Stars в рубли
        stars_rate = Decimal(str(settings.get_stars_rate()))
        rubles = Decimal(config.spin_cost_stars) * stars_rate
        kopeks = int(rubles * 100)

        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)

        if user.balance_kopeks < kopeks:
            raise ValueError('Недостаточно средств на балансе')

        # Списываем с баланса
        user.balance_kopeks -= kopeks
        logger.info(
            '💫 Списано ₽ (⭐) с баланса user_id',
            kopeks=round(kopeks / 100, 2),
            spin_cost_stars=config.spin_cost_stars,
            user_id=user.id,
        )

        return kopeks

    async def _process_days_payment(
        self, db: AsyncSession, user: User, config: WheelConfig, subscription: Subscription | None = None
    ) -> int:
        """
        Обработать оплату днями подписки.
        Возвращает эквивалент в копейках.
        """
        if not subscription:
            if settings.is_multi_tariff_enabled():
                raise ValueError('Необходимо указать подписку для оплаты днями (мульти-тариф)')
            subscription = await get_subscription_by_user_id(db, user.id)

        if not subscription or not subscription.is_active:
            raise ValueError('Нет активной подписки')

        if subscription.days_left < config.min_subscription_days_for_day_payment + config.spin_cost_days:
            raise ValueError('Недостаточно дней подписки')

        # Уменьшаем end_date
        subscription.end_date -= timedelta(days=config.spin_cost_days)
        subscription.updated_at = datetime.now(UTC)

        # Оцениваем стоимость в копейках (для статистики)
        # Берем цену 30-дневного периода и делим на 30
        from app.config import PERIOD_PRICES

        price_30_days = PERIOD_PRICES.get(30, settings.PRICE_30_DAYS) or 19900
        daily_price = price_30_days / 30
        kopeks = int(daily_price * config.spin_cost_days)

        logger.info('📅 Списано дней подписки у user_id', spin_cost_days=config.spin_cost_days, user_id=user.id)

        # Синхронизируем с RemnaWave
        try:
            subscription_service = SubscriptionService()
            result = await subscription_service.update_remnawave_user(db, subscription)
            if result is not None:
                logger.info('✅ Списание дней синхронизировано с RemnaWave для user_id', user_id=user.id)
            else:
                logger.error('⚠️ Не удалось синхронизировать списание дней с RemnaWave', user_id=user.id)
        except Exception as e:
            logger.error('⚠️ Ошибка синхронизации списания дней с RemnaWave', error=e, user_id=user.id)

        return kopeks

    async def _apply_prize(
        self,
        db: AsyncSession,
        user: User,
        prize: WheelPrize,
        config: WheelConfig,
        subscription: Subscription | None = None,
    ) -> str | None:
        """
        Применить приз к пользователю.
        Возвращает промокод (если приз - промокод), иначе None.
        """
        prize_type = prize.prize_type

        if prize_type == WheelPrizeType.NOTHING.value:
            logger.info('🎰 Пустой приз для user_id', user_id=user.id)
            return None

        if prize_type == WheelPrizeType.BALANCE_BONUS.value:
            # Пополнение баланса
            await add_user_balance(
                db,
                user,
                prize.prize_value,
                description=f'Выигрыш в колесе удачи: {prize.prize_value / 100:.2f}₽',
                create_transaction=True,
            )
            logger.info(
                '💰 Начислено ₽ на баланс user_id', prize_value=round(prize.prize_value / 100, 2), user_id=user.id
            )
            return None

        if prize_type == WheelPrizeType.SUBSCRIPTION_DAYS.value:
            # Дни подписки — use provided subscription or fallback
            if not subscription:
                if settings.is_multi_tariff_enabled():
                    # Multi-tariff: нельзя выбрать произвольную подписку, начисляем на баланс
                    await add_user_balance(
                        db,
                        user,
                        prize.prize_value_kopeks,
                        description=f'Выигрыш в колесе удачи: {prize.prize_value} дней (на баланс, мульти-тариф)',
                        create_transaction=True,
                    )
                    logger.info(
                        'Мульти-тариф: дни конвертированы в баланс (подписка не указана)',
                        prize_value=prize.prize_value,
                        user_id=user.id,
                    )
                    return None
                subscription = await get_subscription_by_user_id(db, user.id)
            if subscription:
                # Проверяем суточный тариф - для него конвертируем дни в баланс
                is_daily = getattr(subscription, 'is_daily', False) or (
                    subscription.tariff and getattr(subscription.tariff, 'is_daily', False)
                )

                if is_daily:
                    # Для суточных тарифов: дни * суточная_цена = баланс
                    daily_price = 0
                    if subscription.tariff and hasattr(subscription.tariff, 'daily_price_kopeks'):
                        daily_price = subscription.tariff.daily_price_kopeks or 0

                    if daily_price > 0:
                        balance_bonus = prize.prize_value * daily_price
                        await add_user_balance(
                            db,
                            user,
                            balance_bonus,
                            description=f'Выигрыш в колесе удачи: {prize.prize_value} дней → {balance_bonus / 100:.2f}₽',
                            create_transaction=True,
                        )
                        logger.info(
                            '💰 Суточный тариф: дней конвертированы в ₽ для user_id',
                            prize_value=prize.prize_value,
                            balance_bonus=round(balance_bonus / 100, 2),
                            user_id=user.id,
                        )
                    else:
                        # Если нет цены - используем prize_value_kopeks
                        await add_user_balance(
                            db,
                            user,
                            prize.prize_value_kopeks,
                            description=f'Выигрыш в колесе удачи: {prize.prize_value} дней (на баланс)',
                            create_transaction=True,
                        )
                        logger.info('💰 Дни конвертированы в баланс для user_id', user_id=user.id)
                else:
                    # Обычная подписка - добавляем дни и синхронизируем с RemnaWave
                    subscription.end_date += timedelta(days=prize.prize_value)
                    subscription.updated_at = datetime.now(UTC)
                    logger.info('📅 Начислено дней подписки user_id', prize_value=prize.prize_value, user_id=user.id)

                    # Синхронизируем с RemnaWave
                    try:
                        subscription_service = SubscriptionService()
                        await subscription_service.update_remnawave_user(db, subscription)
                        logger.info('✅ Синхронизировано с RemnaWave для user_id', user_id=user.id)
                    except Exception as e:
                        logger.error('⚠️ Ошибка синхронизации с RemnaWave', error=e)
            else:
                # Если нет подписки - начисляем на баланс эквивалент
                await add_user_balance(
                    db,
                    user,
                    prize.prize_value_kopeks,
                    description=f'Выигрыш в колесе удачи: {prize.prize_value} дней (на баланс)',
                    create_transaction=True,
                )
                logger.info('💰 Дни конвертированы в баланс для user_id', user_id=user.id)
            return None

        if prize_type == WheelPrizeType.TRAFFIC_GB.value:
            # Бонусный трафик — use provided subscription or fallback
            if not subscription:
                if settings.is_multi_tariff_enabled():
                    # Multi-tariff: нельзя выбрать произвольную подписку, начисляем на баланс
                    await add_user_balance(
                        db,
                        user,
                        prize.prize_value_kopeks,
                        description=f'Выигрыш в колесе удачи: {prize.prize_value}GB (на баланс, мульти-тариф)',
                        create_transaction=True,
                    )
                    logger.info(
                        'Мульти-тариф: трафик конвертирован в баланс (подписка не указана)',
                        prize_value=prize.prize_value,
                        user_id=user.id,
                    )
                    return None
                subscription = await get_subscription_by_user_id(db, user.id)
            if subscription and subscription.traffic_limit_gb > 0:
                subscription.traffic_limit_gb += prize.prize_value
                subscription.updated_at = datetime.now(UTC)
                logger.info('📊 Начислено трафика user_id', prize_value=prize.prize_value, user_id=user.id)

                # Синхронизируем с RemnaWave
                try:
                    subscription_service = SubscriptionService()
                    await subscription_service.update_remnawave_user(db, subscription)
                    logger.info('✅ Трафик синхронизирован с RemnaWave для user_id', user_id=user.id)
                except Exception as e:
                    logger.error('⚠️ Ошибка синхронизации трафика с RemnaWave', error=e)
            else:
                # Если безлимит или нет подписки - на баланс
                await add_user_balance(
                    db,
                    user,
                    prize.prize_value_kopeks,
                    description=f'Выигрыш в колесе удачи: {prize.prize_value}GB (на баланс)',
                    create_transaction=True,
                )
            return None

        if prize_type == WheelPrizeType.PROMOCODE.value:
            # Генерация промокода
            promocode = await self._generate_prize_promocode(db, user, prize, config)
            logger.info('🎟️ Сгенерирован промокод для user_id', code=promocode.code, user_id=user.id)
            return promocode.code

        return None

    async def _generate_prize_promocode(
        self, db: AsyncSession, user: User, prize: WheelPrize, config: WheelConfig
    ) -> PromoCode:
        """Сгенерировать уникальный промокод для приза.

        Uses 8 bytes of entropy (16 hex chars) to make collisions vanishingly rare,
        and retries up to 3 times under a savepoint so a collision doesn't poison
        the outer spin transaction.
        """
        if prize.promo_subscription_days > 0:
            promo_type = PromoCodeType.SUBSCRIPTION_DAYS.value
        else:
            promo_type = PromoCodeType.BALANCE.value

        valid_until = datetime.now(UTC) + timedelta(days=config.promo_validity_days)

        for attempt in range(3):
            code = f'{config.promo_prefix}{secrets.token_hex(8).upper()}'
            promocode = PromoCode(
                code=code,
                type=promo_type,
                balance_bonus_kopeks=prize.promo_balance_bonus_kopeks,
                subscription_days=prize.promo_subscription_days,
                max_uses=1,
                valid_until=valid_until,
                is_active=True,
                created_by=user.id,
            )
            try:
                async with db.begin_nested():
                    db.add(promocode)
                    await db.flush()
                return promocode
            except IntegrityError:
                logger.warning(
                    'Promocode collision detected, retrying',
                    attempt=attempt + 1,
                    user_id=user.id,
                )
                if attempt == 2:
                    raise

        raise RuntimeError('Promocode generation retries exhausted')

    async def spin(
        self, db: AsyncSession, user: User, payment_type: str, *, subscription_id: int | None = None
    ) -> SpinResult:
        """
        Выполнить спин колеса.

        Порядок операций:
        1. Cooldown check (SELECT)
        2. Insert WheelSpin with deterministic nonce (UNIQUE constraint = concurrency lock)
        3. Deduct payment (atomic UPDATE for tickets, in-memory for stars/days)
        4. Select prize (_select_prize)
        5. Apply prize (_apply_prize)
        6. Update monthly_wins_count atomically (only if limited prize won)
        7. Commit
        """
        try:
            now = datetime.now(UTC)

            # 1. Anti-abuse cooldown SELECT (graceful "wait" message before INSERT race)
            cooldown_threshold = now - timedelta(seconds=3)
            recent_spin = await db.execute(
                select(WheelSpin.id)
                .where(WheelSpin.user_id == user.id)
                .where(WheelSpin.created_at > cooldown_threshold)
                .limit(1)
            )
            if recent_spin.scalar_one_or_none() is not None:
                return SpinResult(
                    success=False,
                    error='cooldown',
                    message='Подождите перед следующим вращением',
                )

            # Pre-validation: availability, config, prizes, target subscription
            availability = await self.check_availability(db, user)
            if not availability.can_spin:
                return SpinResult(
                    success=False,
                    error=availability.reason,
                    message=self._get_error_message(availability.reason),
                )

            config = await get_or_create_wheel_config(db)
            prizes = await get_wheel_prizes(db, config.id, active_only=True)

            if not prizes:
                return SpinResult(
                    success=False,
                    error='no_prizes',
                    message='Призы не настроены',
                )

            target_subscription = None
            if subscription_id:
                from app.database.crud.subscription import get_subscription_by_id_for_user

                target_subscription = await get_subscription_by_id_for_user(db, subscription_id, user.id)
                if not target_subscription or not target_subscription.is_active:
                    return SpinResult(
                        success=False,
                        error='invalid_subscription',
                        message='Подписка не найдена или неактивна',
                    )
            elif not settings.is_multi_tariff_enabled():
                target_subscription = await get_subscription_by_user_id(db, user.id)

            if (
                settings.is_multi_tariff_enabled()
                and not target_subscription
                and payment_type == WheelSpinPaymentType.SUBSCRIPTION_DAYS.value
            ):
                return SpinResult(
                    success=False,
                    error='subscription_required',
                    message='Выберите подписку для оплаты днями',
                )

            # 2. Insert WheelSpin with deterministic nonce — UNIQUE constraint acts as the
            # real concurrency lock. Two requests from the same user within a 3-second
            # bucket collide on the nonce; only one wins, the other gets IntegrityError.
            spin_nonce = f'{user.id}:{int(now.timestamp() // 3)}'
            wheel_spin = WheelSpin(
                user_id=user.id,
                payment_type=payment_type,
                payment_amount=0,
                payment_value_kopeks=0,
                prize_type=WheelPrizeType.NOTHING.value,
                prize_value=0,
                prize_display_name='',
                prize_value_kopeks=0,
                is_applied=False,
                spin_nonce=spin_nonce,
            )
            db.add(wheel_spin)
            try:
                await db.flush()
            except IntegrityError:
                await db.rollback()
                return SpinResult(
                    success=False,
                    error='cooldown',
                    message='Подождите перед следующим вращением',
                )

            # 3. Process payment
            if payment_type == WheelSpinPaymentType.TELEGRAM_STARS.value:
                if not availability.can_pay_stars:
                    await db.rollback()
                    return SpinResult(
                        success=False,
                        error='cannot_pay_stars',
                        message='Оплата Stars недоступна',
                    )
                payment_amount = config.spin_cost_stars
                payment_value_kopeks = await self._process_stars_payment(db, user, config)
            elif payment_type == WheelSpinPaymentType.SUBSCRIPTION_DAYS.value:
                if not availability.can_pay_days:
                    await db.rollback()
                    return SpinResult(
                        success=False,
                        error='cannot_pay_days',
                        message='Оплата днями подписки недоступна',
                    )
                payment_amount = config.spin_cost_days
                payment_value_kopeks = await self._process_days_payment(db, user, config, target_subscription)
            elif payment_type == WheelSpinPaymentType.TICKETS.value:
                if not config.spin_cost_tickets_enabled:
                    await db.rollback()
                    return SpinResult(
                        success=False,
                        error='cannot_pay_tickets',
                        message='Оплата билетами недоступна',
                    )

                # Anti-abuse: require an active subscription to spend tickets.
                # In multi-tariff target_subscription may be None even when the user
                # has active subs — check the full active list as a fallback.
                has_active_subscription = bool(
                    target_subscription and target_subscription.is_active
                )
                if not has_active_subscription and settings.is_multi_tariff_enabled():
                    from app.database.crud.subscription import (
                        get_active_subscriptions_by_user_id,
                    )

                    active_subs = await get_active_subscriptions_by_user_id(db, user.id)
                    has_active_subscription = any(s.is_active for s in active_subs)
                if not has_active_subscription:
                    await db.rollback()
                    return SpinResult(
                        success=False,
                        error='no_subscription',
                        message='Необходима активная подписка',
                    )

                result = await db.execute(
                    update(User)
                    .where(User.id == user.id, User.spin_tickets >= config.spin_cost_tickets)
                    .values(spin_tickets=User.spin_tickets - config.spin_cost_tickets)
                )
                if result.rowcount == 0:
                    await db.rollback()
                    return SpinResult(
                        success=False,
                        error='insufficient_tickets',
                        message='Недостаточно билетов',
                    )
                await db.flush()
                await db.refresh(user)
                logger.info(
                    '🎟️ Списано билетов с user_id',
                    spin_cost_tickets=config.spin_cost_tickets,
                    user_id=user.id,
                    remaining_tickets=user.spin_tickets,
                )
                payment_amount = config.spin_cost_tickets
                payment_value_kopeks = 0
            else:
                await db.rollback()
                return SpinResult(
                    success=False,
                    error='invalid_payment_type',
                    message='Неверный способ оплаты',
                )

            # 4. Select prize
            prizes_with_probs = self.calculate_prize_probabilities(config, prizes, payment_value_kopeks)
            selected_prize = await self._select_prize(db, user, prizes_with_probs)

            # No candidates after filtering — user paid but gets nothing
            if selected_prize is None:
                wheel_spin.payment_amount = payment_amount
                wheel_spin.payment_value_kopeks = payment_value_kopeks
                wheel_spin.prize_type = WheelPrizeType.NOTHING.value
                wheel_spin.prize_display_name = 'Пусто'
                wheel_spin.is_applied = True
                wheel_spin.applied_at = datetime.now(UTC)
                await db.commit()
                return SpinResult(
                    success=True,
                    prize_type=WheelPrizeType.NOTHING.value,
                    prize_display_name='Пусто',
                    message='К сожалению, в этот раз не повезло. Попробуйте еще!',
                )

            # 5. Apply prize
            generated_promocode = await self._apply_prize(db, user, selected_prize, config, target_subscription)

            # 6. Atomic monthly_wins_count UPDATE — only if prize has a monthly limit.
            # Done AFTER _apply_prize so a failed apply doesn't consume the slot.
            if selected_prize.monthly_limit is not None:
                masked_name = self._mask_username(user)
                claim_result = await db.execute(
                    update(WheelPrize)
                    .where(
                        WheelPrize.id == selected_prize.id,
                        WheelPrize.monthly_wins_count < selected_prize.monthly_limit,
                    )
                    .values(
                        monthly_wins_count=WheelPrize.monthly_wins_count + 1,
                        current_month_winner=masked_name,
                    )
                )
                if claim_result.rowcount == 0:
                    # Race: another request filled the slot first. Prize is already
                    # applied to the user, so we can't undo — just log and continue.
                    logger.warning(
                        'Monthly wins slot exhausted under race; prize still applied',
                        prize_id=selected_prize.id,
                        user_id=user.id,
                    )

            # Rotation for animation, and resolve promocode id
            rotation = self._calculate_rotation(prizes, selected_prize)

            promocode_id = None
            if generated_promocode:
                from sqlalchemy import text

                pc_result = await db.execute(
                    text('SELECT id FROM promocodes WHERE code = :code'),
                    {'code': generated_promocode},
                )
                row = pc_result.fetchone()
                if row:
                    promocode_id = row[0]

            # Finalize the WheelSpin record with actual prize info
            wheel_spin.prize_id = selected_prize.id
            wheel_spin.payment_amount = payment_amount
            wheel_spin.payment_value_kopeks = payment_value_kopeks
            wheel_spin.prize_type = selected_prize.prize_type
            wheel_spin.prize_value = selected_prize.prize_value
            wheel_spin.prize_display_name = selected_prize.display_name
            wheel_spin.prize_value_kopeks = selected_prize.prize_value_kopeks
            wheel_spin.generated_promocode_id = promocode_id
            wheel_spin.is_applied = True
            wheel_spin.applied_at = datetime.now(UTC)

            # 7. Commit
            await db.commit()

            message = self._get_prize_message(selected_prize, generated_promocode)
            return SpinResult(
                success=True,
                prize_id=selected_prize.id,
                prize_type=selected_prize.prize_type,
                prize_value=selected_prize.prize_value,
                prize_display_name=selected_prize.display_name,
                emoji=selected_prize.emoji,
                color=selected_prize.color,
                rotation_degrees=rotation,
                message=message,
                promocode=generated_promocode,
            )

        except ValueError as e:
            await db.rollback()
            return SpinResult(
                success=False,
                error='payment_error',
                message=str(e),
            )
        except Exception as e:
            await db.rollback()
            logger.exception('Ошибка спина колеса для user_id', user_id=user.id, error=e)
            return SpinResult(
                success=False,
                error='internal_error',
                message='Произошла ошибка, попробуйте позже',
            )

    def _get_error_message(self, reason: str | None) -> str:
        """Получить человекочитаемое сообщение об ошибке."""
        messages = {
            'wheel_disabled': 'Колесо удачи временно недоступно',
            'daily_limit_reached': 'Вы достигли лимита спинов на сегодня',
            'no_payment_method_available': 'Нет доступных способов оплаты',
            'no_prizes_configured': 'Призы еще не настроены',
            'insufficient_balance': 'Недостаточно средств на балансе. Пополните баланс для оплаты спина.',
        }
        return messages.get(reason, 'Произошла ошибка')

    def _get_prize_message(self, prize: WheelPrize, promocode: str | None) -> str:
        """Сформировать сообщение о выигрыше."""
        prize_type = prize.prize_type

        if prize_type == WheelPrizeType.NOTHING.value:
            return 'К сожалению, в этот раз не повезло. Попробуйте еще!'

        if prize_type == WheelPrizeType.BALANCE_BONUS.value:
            return f'Поздравляем! Вы выиграли {prize.prize_value / 100:.0f}₽ на баланс!'

        if prize_type == WheelPrizeType.SUBSCRIPTION_DAYS.value:
            days_word = self._pluralize_days(prize.prize_value)
            return f'Поздравляем! Вы выиграли {prize.prize_value} {days_word} подписки!'

        if prize_type == WheelPrizeType.TRAFFIC_GB.value:
            return f'Поздравляем! Вы выиграли {prize.prize_value}GB трафика!'

        if prize_type == WheelPrizeType.PROMOCODE.value:
            return f'Поздравляем! Ваш промокод: {promocode}'

        return 'Поздравляем с выигрышем!'

    def _pluralize_days(self, n: int) -> str:
        """Склонение слова 'день'."""
        if 11 <= n % 100 <= 19:
            return 'дней'
        if n % 10 == 1:
            return 'день'
        if 2 <= n % 10 <= 4:
            return 'дня'
        return 'дней'

    async def get_statistics(
        self, db: AsyncSession, date_from: datetime | None = None, date_to: datetime | None = None
    ) -> dict[str, Any]:
        """Получить статистику колеса."""
        return await get_wheel_statistics(db, date_from, date_to)


# Глобальный экземпляр сервиса
wheel_service = FortuneWheelService()
