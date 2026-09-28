import { lazy, Suspense, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { KnowledgeDocumentsView } from './KnowledgeDocumentsView'
import { useToast } from '../shared/toast-context'

/**
 * `/knowledge` con sus dos mitades.
 *
 * ## Por que son dos vistas y no una
 *
 * Porque responden a dos preguntas distintas y no se mezclan sin perder ninguna de las dos:
 *
 * - **Documentos del workspace**: lo que el cliente escribe sobre sus sistemas. Es lo que el
 *   asistente recupera como contexto.
 * - **Catalogo tecnico**: la guia de remediacion de las familias de fallo, escrita por el
 *   producto. Es lo mismo para todos los clientes y no depende de ninguno.
 *
 * Confundirlas tiene una consecuencia concreta: si el catalogo tecnico alimentara el contexto
 * del asistente, el motor daria por hecho que una regla de remediacion de CWE es una regla del
 * cliente. Y al reves, un documento del cliente sobre su propia autenticacion aparecia en una
 * lista de debilidades genericas, y el usuario no sabria si era consejo del producto o una
 * afirmacion sobre su sistema.
 *
 * ## Por que la de documentos va **primera**
 *
 * Porque es la que el cliente ha creado y la que puede cambiar desde esa misma pantalla. El
 * catalogo tecnico es documentacion de lectura; la pantalla por defecto es la que se mantiene.
 *
 * ## Por que **no** se sustituye una por la otra
 *
 * El enunciado de la tarea pedia sustituir el catalogo de CWE por el gestor documental. No se
 * ha hecho, y la razon es que el catalogo tecnico es una entrega cerrada de la Fase 4 con sus
 * pruebas, su buscador y su ficha de remediacion: borrarlo es una perdida de funcionalidad que
 * no se ve hasta que alguien lo busca. Convivir por pestañas deja las dos a un clic y es
 * reversible en cualquier momento. Si lo que se queria era eliminar el catalogo, es una linea.
 */

/**
 * El catalogo tecnico se carga aparte, y la razon es el orden de peso, no de urls.
 *
 * `/knowledge` es una ruta de navegacion principal: cargarla de forma diferida anadiria una ida
 * y vuelta en cada visita, que es el peaje que un usuario nota al hacer clic en algo del menu.
 * Lo que se separa es la **pestana secundaria**, que es la parte pesada -las tarjetas del
 * catalogo, el modal de detalle y los ejemplos de codigo- y que no aparece en el primer pintado.
 *
 * Abrir `/knowledge` carga al instante los documentos del workspace, que es lo que la pantalla
 * muestra por defecto, y el catalogo llega al pulsarlo.
 */
const KnowledgeCatalogView = lazy(() =>
  import('./KnowledgeCatalogView').then((m) => ({ default: m.KnowledgeCatalogView })),
)

type Vista = 'documentos' | 'catalogo'

export function KnowledgePage() {
  const { t } = useTranslation('knowledge')
  const { notify } = useToast()
  const [vista, setVista] = useState<Vista>('documentos')

  return (
    <section className="page-section" aria-labelledby="knowledge-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
          <h1 id="knowledge-title">{t('title')}</h1>
          <p className="page-description">{t('description')}</p>
        </div>
      </div>

      <div className="filter-bar" role="tablist" aria-label={t('views.label')}>
        <button
          className={vista === 'documentos' ? 'filter-chip filter-chip-active' : 'filter-chip'}
          type="button"
          role="tab"
          aria-selected={vista === 'documentos'}
          onClick={() => setVista('documentos')}
        >
          {t('views.documents')}
        </button>
        <button
          className={vista === 'catalogo' ? 'filter-chip filter-chip-active' : 'filter-chip'}
          type="button"
          role="tab"
          aria-selected={vista === 'catalogo'}
          onClick={() => setVista('catalogo')}
        >
          {t('views.catalog')}
        </button>
      </div>

      {vista === 'documentos' ? (
        <KnowledgeDocumentsView
          onSaved={() => notify('success', t('documents.savedToast'))}
        />
      ) : (
        <Suspense fallback={<p className="table-caption">{t('catalog.loading')}</p>}>
          <KnowledgeCatalogView />
        </Suspense>
      )}
    </section>
  )
}
