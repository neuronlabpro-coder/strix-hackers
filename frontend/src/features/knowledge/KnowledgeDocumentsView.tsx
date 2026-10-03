import { lazy, Suspense, useState } from 'react'
import { FileText, Plus, Search, Trash2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { KnowledgeDocument, KnowledgeDocType } from '../../types/api'
import { Pagination } from '../shared/Pagination'
import {
  EMPTY_DOCUMENTOS_QUERY,
  hayFiltrosPuestos,
  useKnowledgeDocuments,
  type DocumentosQuery,
} from './useKnowledgeDocuments'

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

  /**
   * Los cuatro filtros de la barra, en la fila de los chips de tipo.
   *
   * El buscador y el rango comparten línea con los desplegables de las otras pantallas del
   * panel, y eso no es casualidad: los cuatro llevan etiqueta visible, y con
   * `align-items: start` de `.filter-bar` eso es lo que los deja compartiendo línea de control. Un
   * buscador sin etiqueta sería una fila más corta y quedaría pegado a las etiquetas de al lado,
   * que es justo lo que el `align-self: end` de `.filter-bar > .search-field` viene a arreglar en
   * las pantallas que sí lo llevan sin etiqueta.
   *
   * La regla de "hay filtros" vive en el hook, no aquí: es el hook quien tiene el `query` delante
   * y quien lo comparó con el mismo criterio que el servidor.
   */
  const hayFiltros = hayFiltrosPuestos(docs.query)

  function cambiar(campo: 'search' | 'createdFrom' | 'createdTo', valor: string): void {
    docs.setQuery({ ...docs.query, [campo]: valor } satisfies DocumentosQuery)
  }

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
      {/*
        Los chips de tipo y el botón de añadir van en su **propia** barra, y los filtros de
        texto y fecha en otra debajo.

        Porque los chips son un `role="group"` de botones sin etiqueta, y meterlos en la misma
        fila que los campos con etiqueta no funciona: `.filter-bar` usa `align-items: start` y
        los chips, que son una fila de botones de una línea, quedan pegados a la línea de las
        etiquetas mientras los controles de al lado quedan una línea más abajo. Con el
        desbordamiento, el tercer campo pasaba a la segunda fila y la barra se partía en dos
        escalones.

        Y hay una razón que no es de estilo: los chips son un filtro **distinto** del texto y del
        rango, con su propia forma de activarse —una casilla, no un texto— y separarlos hace que
        se lea «tipo» y «contenido» como dos preguntas. Es la misma razón por la que
        `PentestsPage` y `IssuesPage` no meten el buscador dentro del grupo de estados.
      */}
      <div className="filter-bar">
        <div className="filter-group" role="group" aria-label={t('documents.typeFilterLabel')}>
          <button
            className={
              docs.query.type === null ? 'filter-chip filter-chip-active' : 'filter-chip'
            }
            type="button"
            onClick={() => docs.setQuery({ ...docs.query, type: null })}
          >
            {t('documents.allTypes')}
          </button>
          {TIPOS.map((tipo) => (
            <button
              className={
                docs.query.type === tipo ? 'filter-chip filter-chip-active' : 'filter-chip'
              }
              type="button"
              key={tipo}
              onClick={() =>
                docs.setQuery({ ...docs.query, type: docs.query.type === tipo ? null : tipo })
              }
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
          <Plus size={16} aria-hidden="true" />
          {t('documents.add')}
        </button>
      </div>

      <div className="filter-bar">
        <div className="filter-field filter-field-search">
          <label htmlFor="knowledge-documents-search">{t('documents.filters.search')}</label>
          <span className="search-field">
            <Search size={16} aria-hidden="true" />
            <input
              id="knowledge-documents-search"
              type="search"
              value={docs.query.search}
              placeholder={t('documents.filters.searchPlaceholder')}
              onChange={(event) => cambiar('search', event.target.value)}
            />
          </span>
        </div>

        {/*
          El rango va sobre la fecha de **alta** del documento, no sobre la de última
          modificación que muestra la tarjeta. La explicación va en el `title` de las dos
          etiquetas, que es donde cabe sin romper la alineación de la barra: una línea de ayuda
          dentro del `.filter-field` añadiría altura a un solo campo y descuadraría la fila.
        */}
        <div className="filter-field">
          <label htmlFor="knowledge-documents-created-from" title={t('documents.filters.dateHint')}>
            {t('documents.filters.dateFrom')}
          </label>
          <input
            id="knowledge-documents-created-from"
            type="date"
            value={docs.query.createdFrom}
            onChange={(event) => cambiar('createdFrom', event.target.value)}
          />
        </div>
        <div className="filter-field">
          <label htmlFor="knowledge-documents-created-to" title={t('documents.filters.dateHint')}>
            {t('documents.filters.dateTo')}
          </label>
          <input
            id="knowledge-documents-created-to"
            type="date"
            value={docs.query.createdTo}
            onChange={(event) => cambiar('createdTo', event.target.value)}
          />
        </div>

        {hayFiltros ? (
          <button
            className="ghost-button filter-bar-clear"
            type="button"
            onClick={() => docs.setQuery(EMPTY_DOCUMENTOS_QUERY)}
          >
            <span>{t('documents.filters.clear')}</span>
          </button>
        ) : null}
      </div>

      {docs.loadFailed && (
        <div className="empty-card">
          <p>{t('documents.loadFailed')}</p>
          <button className="secondary-button" type="button" onClick={docs.retry}>
            {t('documents.retry')}
          </button>
        </div>
      )}

      {!docs.loadFailed && docs.isLoading && (
        <p className="table-caption">{t('documents.loading')}</p>
      )}

      {/*
        La tarjeta de vacío distingue «no hay nada» de «el filtro no deja nada». Antes solo
        miraba el tipo, porque era el único filtro que había; con texto y fechas, un listado
        vacío con filtros puestos y un workspace sin documentos son dos pantallas distintas y
        una sola frase para las dos haría que el usuario borrara documentos que sí existen.
      */}
      {!docs.loadFailed && !docs.isLoading && docs.documents.length === 0 && (
        <div className="empty-card">
          <span className="empty-card-mark" aria-hidden="true">
            <FileText size={20} />
          </span>
          <h3>{hayFiltros ? t('documents.emptyFilteredTitle') : t('documents.emptyTitle')}</h3>
          <p>{hayFiltros ? t('documents.emptyFilteredBody') : t('documents.emptyBody')}</p>
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
                    <Trash2 size={17} aria-hidden="true" />
                  </button>
                </header>
                <h3 className="doc-card-title">{documento.title}</h3>
                {/*
                  La descripción viene del servidor, ya extraída del frontmatter, y el párrafo
                  **solo** se pinta si la hay.

                  Antes se sacaba aquí con `extraerParaFormulario(documento.content)`, y el listado
                  **no** devuelve el `content`: lo devuelve el detalle de un documento concreto,
                  porque veinte especificaciones de API serían varios megabytes en una tabla. La
                  expresión leía `undefined.replace(...)` y la pantalla entera reventaba en
                  cuanto el workspace tenía un documento. No se vio porque el workspace de
                  demostración no tenía ninguno, así que la vista nunca pintó una tarjeta.

                  Y si la descripción llega vacía —un documento guardado antes de que el
                  frontmatter fuera estricto es posible en la base— no se pinta el párrafo en vez
                  de repetir el título: el título ya está dos líneas más arriba, y una tarjeta que
                  dice lo mismo dos veces parece un fallo de la tarjeta y no del documento.
                */}
                {documento.description !== '' ? (
                  <p className="doc-card-description">{documento.description}</p>
                ) : null}
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
          {/* La paginación va **debajo** de la rejilla y habla el idioma del endpoint: `total`,
              `limit` y `offset` llegan en la respuesta y el componente no traduce nada de su
              cuenta. Solo aparece si hay más de una página, que es lo que decide el propio
              componente con `total <= limit`. */}
          <Pagination
            total={docs.total}
            limit={docs.limit}
            offset={docs.offset}
            onOffsetChange={docs.setOffset}
            namespace="knowledge"
          />
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
