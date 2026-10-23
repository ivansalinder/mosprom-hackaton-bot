import json
from pathlib import Path
from typing import List, Dict, Any
import numpy as np
from pypdf import PdfReader
from openai import AsyncOpenAI
from monitor.config import settings


class DocumentIndexer:
    def __init__(self) -> None:
        self.client = AsyncOpenAI(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL)
        self.index_path = Path(settings.RAG_INDEX_PATH)
        self.docs_dir = Path(settings.DOCS_DIR)
        self.docs_dir.mkdir(parents=True, exist_ok=True)

    def _extract_chunks_from_pdf(self, pdf_path: Path, chunk_size: int = 900, overlap: int = 150) -> List[Dict[str, Any]]:
        reader = PdfReader(str(pdf_path))
        chunks: List[Dict[str, Any]] = []
        for page_num, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if not text:
                continue
            start = 0
            while start < len(text):
                end = min(start + chunk_size, len(text))
                chunk_text = text[start:end].strip()
                if len(chunk_text) > 50:
                    chunks.append({
                        "source": pdf_path.name,
                        "page": page_num,
                        "text": chunk_text,
                    })
                start += chunk_size - overlap
        return chunks

    async def rebuild_index(self) -> int:
        all_chunks: List[Dict[str, Any]] = []
        for pdf_file in self.docs_dir.glob("*.pdf"):
            all_chunks.extend(self._extract_chunks_from_pdf(pdf_file))
        for txt_file in self.docs_dir.glob("*.txt"):
            text = txt_file.read_text(encoding="utf-8")
            all_chunks.append({"source": txt_file.name, "page": 1, "text": text[:1500]})

        if not all_chunks:
            # Предзаполненные базовые факты из ПВТР П 3.11-07 Группы ПОЛИПЛАСТИК на случай пустой папки
            all_chunks = self._get_default_polyplastic_pvtr_chunks()

        texts = [c["text"] for c in all_chunks]
        resp = await self.client.embeddings.create(model=settings.EMBEDDING_MODEL, input=texts)
        for i, item in enumerate(resp.data):
            all_chunks[i]["embedding"] = item.embedding

        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(json.dumps(all_chunks, ensure_ascii=False), encoding="utf-8")
        return len(all_chunks)

    @staticmethod
    def _get_default_polyplastic_pvtr_chunks() -> List[Dict[str, Any]]:
        """Базовые выдержки из ПВТР П 3.11-07 ООО «Группа ПОЛИПЛАСТИК» для мгновенного старта."""
        return [
            {
                "source": "ПВТР П 3.11-07 (Издание 1)",
                "page": 4,
                "text": (
                    " п. 6.1.3. Документы при приеме на работу: паспорт, трудовая книжка и/или сведения о трудовой "
                    "деятельности, СНИЛС, документы воинского учета (для военнообязанных), документ об образовании. "
                    "Дополнительно для льгот и вычетов: ИНН, свидетельства о браке/рождении детей."
                ),
            },
            {
                "source": "ПВТР П 3.11-07 (Издание 1)",
                "page": 5,
                "text": (
                    "п. 6.1.9. Срок испытания при приеме на работу не может превышать 3 месяцев (для руководителей "
                    "организации, главных бухгалтеров и их заместителей, руководителей филиалов — 6 месяцев). "
                    "В компании действует Кадровый электронный документооборот (КЭДО, п. 6.1.26)."
                ),
            },
            {
                "source": "ПВТР П 3.11-07 (Издание 1)",
                "page": 15,
                "text": (
                    "п. 6.5.2. Заработная плата выплачивается каждые полмесяца: 29-го числа текущего месяца "
                    "(в феврале — 28-го числа) за первую половину месяца и 14-го числа месяца, следующего за "
                    "отработанным, за вторую половину месяца. Если день выплаты выпадает на выходной, выплата "
                    "производится накануне."
                ),
            },
            {
                "source": "ПВТР П 3.11-07 (Издание 1)",
                "page": 15,
                "text": (
                    "п. 6.6.1. Режим рабочего времени: пятидневная рабочая неделя с двумя выходными (суббота, воскресенье). "
                    "С понедельника по четверг: начало работы в 9:00, окончание в 18:00. В пятницу: с 9:00 до 17:00. "
                    "Перерыв для отдыха и питания: с 13:00 до 13:48."
                ),
            },
            {
                "source": "ПВТР П 3.11-07 (Издание 1)",
                "page": 17,
                "text": (
                    "п. 6.7.4 - 6.7.11. Ежегодный основной оплачиваемый отпуск составляет 28 календарных дней. "
                    "Право на отпуск за первый год возникает по истечении 6 месяцев непрерывной работы. "
                    "При разделении отпуска на части хотя бы одна из частей должна быть не менее 14 календарных дней."
                ),
            },
            {
                "source": "ПВТР П 3.11-07 (Издание 1)",
                "page": 14,
                "text": (
                    "п. 6.4.2 и п. 6.11.1. В случае невыхода на работу по болезни (временной нетрудоспособности) "
                    "работник обязан незамедлительно известить непосредственного руководителя любым доступным способом "
                    "(телефон, email). Пособие за первые 3 дня болезни выплачивает работодатель на основании ЭЛН, "
                    "с 4-го дня — СФР."
                ),
            },
        ]