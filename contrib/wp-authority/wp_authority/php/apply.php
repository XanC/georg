<?php
// SPDX-License-Identifier: AGPL-3.0-only
// Apply a plan's actions. Each action is checked against the current state first, so a
// rerun is a no-op and a sermon edited since the plan was made is skipped, not overwritten.
$payload = json_decode( base64_decode( '@PAYLOAD@' ), true );
$refs    = array();
$results = array();

function wpa_ref( $ref, $refs ) {
	return is_int( $ref ) ? $ref : ( isset( $refs[ $ref ] ) ? $refs[ $ref ] : null );
}

function wpa_find( $name, $taxonomy ) {
	$found = get_terms(
		array(
			'taxonomy'   => $taxonomy,
			'name'       => $name,
			'hide_empty' => false,
			'orderby'    => 'term_id',
		)
	);
	return ( is_wp_error( $found ) || ! $found ) ? null : (int) $found[0]->term_id;
}

function wpa_ids( $post, $taxonomy ) {
	$ids = wp_get_object_terms( $post, $taxonomy, array( 'fields' => 'ids' ) );
	$ids = array_map( 'intval', $ids );
	sort( $ids );
	return $ids;
}

foreach ( $payload['actions'] as $action ) {
	$tax    = $action['taxonomy'];
	$result = 'done';
	switch ( $action['op'] ) {
		case 'create':
			$id = wpa_find( $action['name'], $tax );
			if ( null === $id ) {
				$made = wp_insert_term( $action['name'], $tax );
				$id   = is_wp_error( $made ) ? null : (int) $made['term_id'];
				$result = is_wp_error( $made ) ? 'error: ' . $made->get_error_message() : 'done';
			} else {
				$result = 'exists';
			}
			$refs[ $action['ref'] ] = $id;
			break;
		case 'rename':
			$term = get_term( $action['term'], $tax );
			if ( ! $term || is_wp_error( $term ) ) {
				$result = 'skipped: term missing';
			} elseif ( html_entity_decode( $term->name, ENT_QUOTES ) === $action['name'] ) {
				$result = 'unchanged';
			} else {
				$done   = wp_update_term( $term->term_id, $tax, array( 'name' => $action['name'] ) );
				$result = is_wp_error( $done ) ? 'error: ' . $done->get_error_message() : 'done';
			}
			break;
		case 'parent':
			$id     = wpa_ref( $action['term'], $refs );
			$parent = wpa_ref( $action['parent'], $refs );
			$term   = $id ? get_term( $id, $tax ) : null;
			if ( ! $term || is_wp_error( $term ) || null === $parent ) {
				$result = 'skipped: term missing';
			} elseif ( (int) $term->parent === $parent ) {
				$result = 'unchanged';
			} else {
				$done   = wp_update_term( $id, $tax, array( 'parent' => $parent ) );
				$result = is_wp_error( $done ) ? 'error: ' . $done->get_error_message() : 'done';
			}
			break;
		case 'describe':
			$id   = wpa_ref( $action['term'], $refs );
			$term = $id ? get_term( $id, $tax ) : null;
			if ( ! $term || is_wp_error( $term ) ) {
				$result = 'skipped: term missing';
			} elseif ( $term->description === $action['description'] ) {
				$result = 'unchanged';
			} else {
				$done   = wp_update_term( $id, $tax, array( 'description' => $action['description'] ) );
				$result = is_wp_error( $done ) ? 'error: ' . $done->get_error_message() : 'done';
			}
			break;
		case 'assign':
			$after = array();
			foreach ( $action['after'] as $ref ) {
				$after[] = wpa_ref( $ref, $refs );
			}
			$before = $action['before'];
			sort( $before );
			$want = $after;
			sort( $want );
			$now = wpa_ids( $action['post'], $tax );
			if ( ! get_post( $action['post'] ) ) {
				$result = 'skipped: post missing';
			} elseif ( in_array( null, $after, true ) ) {
				$result = 'skipped: term missing';
			} elseif ( $now === $want ) {
				$result = 'unchanged';
			} elseif ( $now !== $before ) {
				$result = 'skipped: changed since the plan was made';
			} else {
				$done   = wp_set_object_terms( $action['post'], $after, $tax );
				$result = is_wp_error( $done ) ? 'error: ' . $done->get_error_message() : 'done';
			}
			break;
		case 'delete':
			$term = get_term( $action['term'], $tax );
			if ( ! $term || is_wp_error( $term ) ) {
				$result = 'unchanged';
			} elseif ( get_objects_in_term( $term->term_id, $tax ) ) {
				$result = 'skipped: still in use';
			} elseif ( get_terms( array( 'taxonomy' => $tax, 'parent' => $term->term_id, 'hide_empty' => false, 'fields' => 'ids' ) ) ) {
				$result = 'skipped: has child terms';
			} else {
				$done   = wp_delete_term( $term->term_id, $tax );
				$result = ( true === $done ) ? 'done' : 'error: delete failed';
			}
			break;
		default:
			$result = 'error: unknown op';
	}
	$results[] = array( 'action' => $action, 'result' => $result );
}
echo "\n@@WP-AUTHORITY@@" . wp_json_encode( array( 'results' => $results, 'refs' => (object) $refs ) ) . "\n";
