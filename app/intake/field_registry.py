from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from app.intake.models import ProcurementType, RequestDraftData

QuestionType = Literal["free_text", "decimal", "date", "boolean", "choice"]
RequiredPredicate = Callable[[RequestDraftData], bool]


@dataclass(frozen=True)
class FieldDefinition:
    code: str
    label: str
    data_type: str = "string"
    required_scope: str = "optional"
    required_for_procurement_types: tuple[ProcurementType, ...] = ()
    required_for_categories: tuple[str, ...] = ()
    required_when: str | None = None
    question: str = ""
    clarification_question: str = ""
    priority: int = 100
    display_order: int = 100
    card_section: str = "Потребность"
    sensitive: bool = False
    validator_name: str = "text"
    allows_explicit_correction: bool = True
    dependencies: tuple[str, ...] = ()
    question_type: QuestionType = "free_text"
    options: tuple[str, ...] = ()


CATEGORY_NAMES = {
    "G01": "Офисные принадлежности",
    "G02": "Мебель и оснащение",
    "G03": "IT-оборудование",
    "G04": "IT-периферия",
    "G05": "ПО и лицензии",
    "G06": "Складское оборудование",
    "G07": "Погрузочная техника и запчасти",
    "G08": "Спецодежда и СИЗ",
    "G09": "Хозяйственные товары",
    "G10": "Упаковочные материалы",
    "G11": "Полиграфическая продукция",
    "G12": "Рекламные и POS-материалы",
    "G13": "Сувенирная продукция",
    "G14": "Электротехнические материалы",
    "G15": "Инженерные запчасти",
    "S01": "Ремонт и обслуживание",
    "S02": "Клининговые услуги",
    "S03": "Транспортные услуги",
    "S04": "Складские услуги",
    "S05": "IT-разработка и поддержка",
    "S06": "Маркетинговые услуги",
    "S07": "Дизайн и контент",
    "S08": "Организация мероприятий",
    "S09": "Обучение",
    "S10": "Подбор персонала",
    "S11": "Консалтинг и юридические услуги",
    "S12": "Переводческие услуги",
    "S13": "Полиграфические услуги",
    "S14": "Аренда",
    "S15": "Прочие профессиональные услуги",
}


def _brand_restricted(draft: RequestDraftData) -> bool:
    return bool(draft.preferred_brand) and draft.analogs_allowed is False


def _single_supplier(draft: RequestDraftData) -> bool:
    return draft.single_supplier is True


def _urgent(draft: RequestDraftData) -> bool:
    return draft.urgency in {"P1", "P2"}


def _physical_location_required(draft: RequestDraftData) -> bool:
    return (
        draft.procurement_type == ProcurementType.WORK
        or (
            draft.procurement_type == ProcurementType.GOODS
            and draft.category_code != "G05"
        )
        or (
            draft.procurement_type == ProcurementType.SERVICE
            and draft.work_on_site is True
        )
    )


PREDICATES: dict[str, RequiredPredicate] = {
    "brand_restricted": _brand_restricted,
    "single_supplier": _single_supplier,
    "urgent": _urgent,
    "physical_location_required": _physical_location_required,
}


class RequestFieldRegistry:
    def __init__(self) -> None:
        self._fields = _definitions()
        if len(self._fields) != len({item.code for item in self._fields}):
            raise ValueError("Intake field codes must be unique")

    def all(self) -> tuple[FieldDefinition, ...]:
        return tuple(self._fields)

    def get(self, code: str) -> FieldDefinition | None:
        return next((item for item in self._fields if item.code == code), None)

    def applicable(self, draft: RequestDraftData) -> list[FieldDefinition]:
        result = []
        for item in self._fields:
            type_match = (
                not item.required_for_procurement_types
                or draft.procurement_type in item.required_for_procurement_types
            )
            category_match = (
                not item.required_for_categories
                or draft.category_code in item.required_for_categories
            )
            if item.required_scope != "category" or category_match:
                if item.required_scope != "type" or type_match:
                    result.append(item)
        return result

    def is_required(self, item: FieldDefinition, draft: RequestDraftData) -> bool:
        if item.required_scope == "always":
            return True
        if item.required_scope == "readiness":
            return True
        if item.required_scope == "type":
            return draft.procurement_type in item.required_for_procurement_types
        if item.required_scope == "category":
            return draft.category_code in item.required_for_categories
        if item.required_when:
            return PREDICATES[item.required_when](draft)
        return False


def _f(
    code: str,
    label: str,
    question: str,
    priority: int,
    order: int,
    **kwargs,
) -> FieldDefinition:
    return FieldDefinition(
        code=code,
        label=label,
        question=question,
        clarification_question=question,
        priority=priority,
        display_order=order,
        **kwargs,
    )


def _definitions() -> list[FieldDefinition]:
    always = {"required_scope": "always"}
    goods = {
        "required_scope": "type",
        "required_for_procurement_types": (ProcurementType.GOODS,),
    }
    return [
        _f(
            "request_id",
            "Идентификатор заявки",
            "",
            200,
            1,
            allows_explicit_correction=False,
        ),
        _f(
            "requester_id",
            "Инициатор",
            "",
            200,
            2,
            allows_explicit_correction=False,
            sensitive=True,
        ),
        _f(
            "procurement_type",
            "Тип закупки",
            "Это товар, услуга или работа?",
            10,
            10,
            validator_name="procurement_type",
            question_type="choice",
            options=("goods", "service", "work"),
            **always,
        ),
        _f(
            "item_name",
            "Предмет закупки",
            "Что требуется закупить?",
            20,
            20,
            **always,
        ),
        _f(
            "description",
            "Описание потребности",
            "Опишите потребность или состав работ.",
            30,
            30,
            required_scope="type",
            required_for_procurement_types=(
                ProcurementType.SERVICE,
                ProcurementType.WORK,
            ),
        ),
        _f(
            "category_code",
            "Категория",
            "Выберите наиболее подходящую категорию закупки.",
            40,
            40,
            validator_name="category",
            dependencies=("procurement_type",),
            question_type="choice",
            options=tuple(f"{code} — {name}" for code, name in CATEGORY_NAMES.items()),
            **always,
        ),
        _f(
            "title",
            "Название",
            "Укажите краткое название заявки, если оно отличается от предмета.",
            950,
            50,
        ),
        _f(
            "quantity",
            "Количество",
            "Укажите количество.",
            50,
            60,
            data_type="decimal",
            validator_name="quantity",
            question_type="decimal",
            **goods,
        ),
        _f("unit", "Единица измерения", "Укажите единицу измерения.", 51, 70, **goods),
        _f(
            "specifications",
            "Характеристики / объём",
            "Укажите характеристики или объём работ.",
            60,
            80,
            required_scope="type",
            required_for_procurement_types=(
                ProcurementType.GOODS,
                ProcurementType.SERVICE,
                ProcurementType.WORK,
            ),
        ),
        _f(
            "desired_result",
            "Ожидаемый результат",
            "Какой результат должен быть получен?",
            61,
            90,
            required_scope="category",
            required_for_categories=("S11",),
        ),
        _f(
            "preferred_brand",
            "Предпочтительный бренд",
            "Есть ли предпочтительный бренд?",
            89,
            100,
        ),
        _f(
            "analogs_allowed",
            "Разрешены аналоги",
            "Допустимы ли аналоги?",
            62,
            110,
            data_type="boolean",
            validator_name="boolean",
            question_type="boolean",
            options=("Да", "Нет"),
            required_scope="category",
            required_for_categories=("G03",),
        ),
        _f(
            "brand_justification",
            "Обоснование бренда",
            "Почему нельзя предложить аналог?",
            63,
            120,
            required_when="brand_restricted",
        ),
        _f(
            "amount",
            "Сумма",
            "Укажите общую сумму закупки.",
            70,
            130,
            data_type="decimal",
            validator_name="amount",
            question_type="decimal",
            **always,
        ),
        _f("currency", "Валюта", "", 200, 135),
        _f(
            "budget_status",
            "Бюджетный статус",
            "Закупка бюджетная или внебюджетная?",
            71,
            140,
            validator_name="budget_status",
            question_type="choice",
            options=("budgeted", "unbudgeted"),
            **always,
        ),
        _f(
            "desired_delivery_date",
            "Требуемая дата",
            "К какой дате нужна поставка или результат?",
            80,
            150,
            data_type="date",
            validator_name="date",
            question_type="date",
            **always,
        ),
        _f(
            "delivery_location",
            "Место поставки / работ",
            "Укажите место поставки или выполнения работ.",
            81,
            160,
            required_when="physical_location_required",
        ),
        _f(
            "single_supplier",
            "Единственный поставщик",
            "Закупка у единственного поставщика?",
            90,
            170,
            data_type="boolean",
            validator_name="boolean",
            question_type="boolean",
            options=("Да", "Нет"),
        ),
        _f(
            "supplier_name",
            "Поставщик",
            "Укажите поставщика.",
            91,
            180,
            required_when="single_supplier",
        ),
        _f(
            "single_supplier_justification",
            "Обоснование поставщика",
            "Обоснуйте выбор единственного поставщика.",
            92,
            190,
            required_when="single_supplier",
        ),
        _f(
            "urgency",
            "Срочность",
            "Укажите приоритет P1–P4, если он нужен.",
            93,
            200,
            validator_name="urgency",
            question_type="choice",
            options=("P1", "P2", "P3", "P4"),
        ),
        _f(
            "urgency_justification",
            "Обоснование срочности",
            "Обоснуйте срочность P1/P2.",
            94,
            210,
            required_when="urgent",
        ),
        _f(
            "has_data_access",
            "Доступ к данным",
            "Нужен ли доступ к данным компании?",
            64,
            220,
            data_type="boolean",
            validator_name="boolean",
            question_type="boolean",
            options=("Да", "Нет"),
            required_scope="category",
            required_for_categories=("S05",),
        ),
        _f(
            "work_on_site",
            "Работы на площадке",
            "Работы выполняются на территории компании?",
            95,
            230,
            data_type="boolean",
            validator_name="boolean",
            question_type="boolean",
            options=("Да", "Нет"),
        ),
        _f(
            "business_justification",
            "Обоснование потребности",
            "Зачем нужна эта закупка?",
            82,
            240,
            **always,
        ),
        _f(
            "department",
            "Подразделение",
            "Укажите подразделение-заказчика.",
            990,
            250,
            required_scope="readiness",
        ),
        _f(
            "contact_person",
            "Контактное лицо",
            "Укажите контактное лицо.",
            991,
            260,
            required_scope="readiness",
        ),
        _f(
            "comments", "Комментарий", "Добавьте комментарий, если требуется.", 110, 270
        ),
    ]
