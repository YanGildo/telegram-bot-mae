"""Estado de 'esperando você digitar algo' usado pelas telas do painel do filho."""

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton

from ..ui import kb, show


class ChildFlow(StatesGroup):
    add_admin = State()
    input = State()  # texto digitado; o tipo fica em data["kind"]
    media = State()  # recebendo fotos/vídeos de uma mensagem repetida


async def ask(cb: CallbackQuery, state: FSMContext, kind: str, prompt: str, cancel: InlineKeyboardButton,
              **data) -> None:
    await state.set_state(ChildFlow.input)
    await state.update_data(kind=kind, **data)
    await cb.answer()
    await show(cb, (prompt + "\n\n/cancelar para desistir.", kb([cancel])))
