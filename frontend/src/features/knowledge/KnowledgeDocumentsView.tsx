import { lazy, Suspense, useState } from 'react'
import { FileText, Plus, Trash2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { KnowledgeDocument, KnowledgeDocType } from '../../types/api'
import { extraerParaFormulario } from './OkfValidator'
import { useKnowledgeDocuments } from './useKnowledgeDocuments'

/**
 * Los documentos del workspace: lo que el asistente usa como contexto.
 *
 * ## Por qué la descripción se lee del frontmatter y no de la columna `title`
 *
 * Porque la columna `title` es lo que el cliente escribió al guardar, y el frontmatter puede
 * haberse editado después desde el panel. La `description` es lo que el motor inyecta cuando el
 * documento se cita sin su cuerpo entero, así que es lo que el usuario tiene que ver para saber
 * qué está entrando en el contexto.
 *
 * Si no se puede leer, se muestra el título y no un hueco: un documento sin `description` es
 * posible en la base —el backend acepta documentos guardados antes de que el formato fuera
 * estricto— y vaciar la tarjeta dejaría al usuario pensando que el documento no existe.
 */

/**
 * El modal de edicion OKF se carga aparte.
 *
 * Son el editor, el formulario con sus cinco campos y el validador de frontmatter: casi nueve
 * kilobytes de JavaScript que **solo** hacen falta cuando alguien pulsa "Anadir documento".
 *
 * Pesa mas de lo que parece porque lleva la logica de parseo del formato, que esta replicada
 * del backend a proposito para poder avisar del error mientras se escribe en vez de despues de
 * pulsar guardar. Esa replica es el motivo de que el modulo pese, y tambien el motivo de que no
 * haya que pagar su coste en la carga de una pantalla que casi siempre se abre para *leer*.
 */
const OkfEditorModal = lazy(() =>
  import('./OkfEditorModal').then((m) => ({ default: m.OkfEditorModal })),
)

const TIPOS: KnowledgeDocType[] = [
  'DOCUMENTATION',
  'BUSINESS_RULE',
  'API_SPEC',
  'ARCHITECTURE',
]

/** La clase del badge por tipo. El color es por *naturaleza*, no por gravedad. */
const BADGE_POR_TIPO: Record<KnowledgeDocType, string> = {
  DOCUMENTATION: 'doc-type-documentation',
  BUSINESS_RULE: 'doc-type-business-rule',
  API_SPEC: 'doc-type-api-spec',
  ARCHITECTURE: 'doc-type-architecture',
}

export interface KnowledgeDocumentsViewProps {
  onSaved: () => void
}

export function KnowledgeDocumentsView({ onSaved }: KnowledgeDocumentsViewProps) {
  const { t } = useTranslation('knowledge')
  const docs = useKnowledgeDocuments()
  const [isEditorOpen, setIsEditorOpen] = useState(false)
  const [pendingDelete, setPendingDelete] = useState<KnowledgeDocument | null>(null)

  async function guardar(payload: {
    title: string
    doc_type: KnowledgeDocType
    content: string
  }): Promise<boolean> {
    const ok = await docs.create(payload)
    if (ok) onSaved()
    return ok
  }

  async function confirmarBorrado(): Promise<void> {
    if (pendingDelete === null) return
    const objetivo = pendingDelete
    setPendingDelete(null)
    await docs.remove(objetivo.id)
  }

  return (
    <div className="content-card">
      <div className="filter-bar">
        <div className="filter-group" role="group" aria-label={t('documents.typeFilterLabel')}>
          <button
            className={
              docs.filterType === null ? 'filter-chip filter-chip-active' : 'filter-chip'
            }
            type="button"
            onClick={() => docs.setFilterType(null)}
          >
            {t('documents.allTypes')}
          </button>
          {TIPOS.map((tipo) => (
            <button
              className={
                docs.filterType === tipo ? 'filter-chip filter-chip-active' : 'filter-chip'
              }
              type="button"
              key={tipo}
              onClick={() => docs.setFilterType(docs.filterType === tipo ? null : tipo)}
            >
              {t(`documents.types.${tipo}`)}
            </button>
          ))}
        </div>

        <button
          className="primary-button"
          type="button"
          onClick={() => setIsEditorOpen(true)}
        >
          <Plus size={15} aria-hidden="true" />
          {t('documents.add')}
        </button>
      </div>

      {docs.loadFailed && (
        <div className="empty-state">
          <p>{t('documents.loadFailed')}</p>
          <button className="secondary-button" type="button" onClick={docs.retry}>
            {t('documents.retry')}
          </button>
        </div>
      )}

      {!docs.loadFailed && docs.isLoading && (
        <p className="table-caption">{t('documents.loading')}</p>
      )}

      {!docs.loadFailed && !docs.isLoading && docs.documents.length === 0 && (
        <div className="empty-state">
          <FileText size={26} aria-hidden="true" />
          <h3>
            {docs.filterType === null
              ? t('documents.emptyTitle')
              : t('documents.emptyFilteredTitle')}
          </h3>
          <p>
            {docs.filterType === null ? t('documents.emptyBody') : t('documents.emptyFilteredBody')}
          </p>
        </div>
      )}

      {!docs.loadFailed && !docs.isLoading && docs.documents.length > 0 && (
        <>
          <div className="doc-grid">
            {docs.documents.map((documento) => (
              <article className="doc-card" key={documento.id}>
                <header className="doc-card-header">
                  <span className={BADGE_POR_TIPO[documento.doc_type]}>
                    {t(`documents.types.${documento.doc_type}`)}
                  </span>
                  {/* El boton de borrar va en la tarjeta y no en un menu. Es una de las dos
                      acciones de la vista y esconderla tras un menu convierte "borrar" en una
                      accion de tres clics para algo que el usuario ha pedido viendo una
                      tarjeta. */}
                  <button
                    className="icon-button doc-card-delete"
                    type="button"
                    onClick={() => setPendingDelete(documento)}
                    aria-label={t('documents.deleteAria', { title: documento.title })}
                  >
                    <Trash2 size={15} aria-hidden="true" />
                  </button>
                </header>
                <h3 className="doc-card-title">{documento.title}</h3>
                <p className="doc-card-description">
                  {/* La descripcion se saca del frontmatter y no de la primera linea del
                      contenido: la primera linea de un documento OKF es `---`, y mostrarla
                      haria que todas las tarjetas se vieran iguales. */}
                  {extraerParaFormulario(documento.content).description}
                </p>
                <footer className="doc-card-footer">
                  <time dateTime={documento.updated_at}>
                    {new Date(documento.updated_at).toLocaleDateString()}
                  </time>
                </footer>
              </article>
            ))}
          </div>
          <p className="table-caption">
            {t('documents.count', { shown: docs.documents.length, total: docs.total })}
          </p>
        </>
      )}

      {pendingDelete !== null && (
        <div className="modal-backdrop" role="presentation" onClick={() => setPendingDelete(null)}>
          <div
            className="modal modal-confirm"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="doc-delete-title"
            onClick={(event) => event.stopPropagation()}
          >
            <h2 id="doc-delete-title">{t('documents.deleteTitle')}</h2>
            <p>{t('documents.deleteBody', { title: pendingDelete.title })}</p>
            <footer className="modal-footer">
              <button className="secondary-button" type="button" onClick={() => setPendingDelete(null)}>
                {t('documents.deleteCancel')}
              </button>
              <button className="danger-button" type="button" onClick={() => void confirmarBorrado()}>
                <Trash2 size={15} aria-hidden="true" />
                {t('documents.deleteConfirm')}
              </button>
            </footer>
          </div>
        </div>
      )}

      {isEditorOpen && (
        <Suspense fallback={null}>
          <OkfEditorModal
            onClose={() => setIsEditorOpen(false)}
            onSave={guardar}
            isSaving={docs.isSaving}
            saveFailed={docs.saveFailed}
          />
        </Suspense>
      )}
    </div>
  )
}
