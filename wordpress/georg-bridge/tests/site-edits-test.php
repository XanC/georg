<?php
// SPDX-License-Identifier: AGPL-3.0-only
/**
 * Command-line tests for the site-edit helpers in includes/sermon.php:
 * `php tests/site-edits-test.php`. Exits non-zero on failure.
 *
 * @package GeorgBridge
 */

require __DIR__ . '/../includes/sermon.php';

$georg_failures = 0;

/**
 * Records one check.
 *
 * @param bool   $ok   Whether it passed.
 * @param string $what Description.
 */
function georg_check( $ok, $what ) {
	global $georg_failures;
	echo ( $ok ? 'PASS ' : 'FAIL ' ) . $what . "\n";
	if ( ! $ok ) {
		++$georg_failures;
	}
}

$georg_fp = georg_bridge_fingerprint( 'Sermon for St. Luke' );
georg_check( 64 === strlen( $georg_fp ) && georg_bridge_fingerprint( 'Sermon for St. Luke' ) === $georg_fp, 'fingerprint is a stable sha256' );
georg_check( georg_bridge_fingerprint( array( 3, 7 ) ) !== georg_bridge_fingerprint( array( 7, 3 ) ), 'term order matters, so callers sort' );

$georg_written = array(
	'title'                 => georg_bridge_fingerprint( 'Sermon for 10/25' ),
	'content'               => georg_bridge_fingerprint( '<p>text</p>' ),
	'terms:ctc_sermon_book' => georg_bridge_fingerprint( array( 1, 2 ) ),
);
$georg_current = array(
	'title'                 => georg_bridge_fingerprint( 'Sermon For 10/25' ),
	'content'               => $georg_written['content'],
	'date'                  => georg_bridge_fingerprint( array( 'x', 'y' ) ),
	'terms:ctc_sermon_book' => georg_bridge_fingerprint( array( 1, 2, 9 ) ),
);
georg_check(
	array( 'terms:ctc_sermon_book', 'title' ) === georg_bridge_changed_fields( $georg_current, $georg_written ),
	'changed fields are the ones whose stored value differs'
);
georg_check( array() === georg_bridge_changed_fields( $georg_written, $georg_written ), 'nothing changed, nothing edited' );

georg_check(
	array( 'title' ) === georg_bridge_conflicts( array( 'title', 'date', 'audio' ), array( 'title', 'terms:ctc_sermon_book' ), array() ),
	'only requested edited fields conflict'
);
georg_check( array() === georg_bridge_conflicts( array( 'title' ), array( 'title' ), array( 'title' ) ), 'overwrite clears a conflict' );
georg_check( array() === georg_bridge_conflicts( array( 'date' ), array( 'title' ), array() ), 'skipping the edited field avoids the conflict' );

// Posts written before 0.4.0, from the live site: bridge user 4 (scripts), owner user 2 (xan).
// 8228: Georg wrote it, then the owner saved at 10:56:03 CST (no revision, only an
// autosave), leaving _edit_last = 2 and post_modified_gmt after Georg's write.
georg_check( true === georg_bridge_legacy_edited( 2, 4, '2026-11-13 16:56:03', strtotime( '2026-11-13T16:11:52Z' ) ), 'post 8228: saved by the owner after Georg wrote it is an edit' );
// 8212: _edit_last = 2 from an older edit, but Georg rewrote the post at 10:11:52 CST, so
// post_modified_gmt is Georg's own write; Georg records its time just after the save.
georg_check( false === georg_bridge_legacy_edited( 2, 4, '2026-11-13 16:11:52', strtotime( '2026-11-13T16:11:53Z' ) ), 'post 8212: Georg rewriting after the edit settles it' );
georg_check( false === georg_bridge_legacy_edited( 2, 4, '2026-11-13 16:11:52', strtotime( '2026-11-13T16:11:52Z' ) ), 'modified exactly at Georg\'s write is not an edit' );
georg_check( false === georg_bridge_legacy_edited( 2, 4, '2026-11-13 16:12:02', strtotime( '2026-11-13T16:11:52Z' ) ), 'within the tolerance after Georg\'s write is not an edit' );
georg_check( true === georg_bridge_legacy_edited( 2, 4, '2026-11-13 16:12:03', strtotime( '2026-11-13T16:11:52Z' ) ), 'past the tolerance is an edit' );
// 8214 ... 8226: no _edit_last.
georg_check( false === georg_bridge_legacy_edited( 0, 4, '2026-11-13 16:56:03', strtotime( '2026-11-13T16:11:52Z' ) ), 'no _edit_last (posts 8214-8226) is not an edit' );
georg_check( false === georg_bridge_legacy_edited( 4, 4, '2026-11-13 16:56:03', strtotime( '2026-11-13T16:11:52Z' ) ), '_edit_last by the bridge user is not an edit' );
georg_check( true === georg_bridge_legacy_edited( 2, 4, '2026-11-13 16:11:52', null ), 'without Georg\'s write time a non-bridge _edit_last is an edit' );

georg_check( null === georg_bridge_parse_time( null ) && null === georg_bridge_parse_time( '' ), 'absent georg_written_at' );
georg_check( strtotime( '2026-11-13T16:11:52Z' ) === georg_bridge_parse_time( '2026-11-13T16:11:52Z' ), 'parses UTC' );
georg_check( strtotime( '2026-11-13T16:11:52Z' ) === georg_bridge_parse_time( '2026-11-13T10:11:52-06:00' ), 'parses an offset' );
georg_check( false === georg_bridge_parse_time( '2026-11-13 16:11:52' ) && false === georg_bridge_parse_time( 'yesterday' ), 'a time without a zone is invalid' );

$georg_taxonomies = array( 'ctc_sermon_book', 'ctc_sermon_series' );
georg_check( null === georg_bridge_parse_fields( null, $georg_taxonomies ), 'absent fields means all' );
georg_check(
	array( 'title', 'audio', 'terms:ctc_sermon_book' ) === georg_bridge_parse_fields( array( 'title', 'audio', 'terms:ctc_sermon_book', 'title' ), $georg_taxonomies ),
	'known fields are accepted and deduplicated'
);
georg_check( false === georg_bridge_parse_fields( array( 'terms:category' ), $georg_taxonomies ), 'unknown taxonomy is rejected' );
georg_check( false === georg_bridge_parse_fields( array( 'excerpt' ), $georg_taxonomies ), 'unknown field is rejected' );
georg_check( false === georg_bridge_parse_fields( 'title', $georg_taxonomies ), 'a non-list is rejected' );

$georg_next = georg_bridge_next_written( $georg_written, $georg_current, array( 'date', 'terms:ctc_sermon_book' ), false );
georg_check( $georg_written['title'] === $georg_next['title'], 'a skipped edited field keeps the old fingerprint, so it stays edited' );
georg_check( $georg_current['date'] === $georg_next['date'] && $georg_current['terms:ctc_sermon_book'] === $georg_next['terms:ctc_sermon_book'], 'written fields get the read-back fingerprint' );
$georg_legacy = georg_bridge_next_written( array(), $georg_current, array( 'date' ), true );
georg_check( 'edited-on-site' === $georg_legacy['title'] && $georg_current['date'] === $georg_legacy['date'], 'an edited pre-0.4.0 post keeps skipped fields edited' );
$georg_clean = georg_bridge_next_written( array(), $georg_current, array( 'date' ), false );
georg_check( $georg_current['title'] === $georg_clean['title'], 'an unedited pre-0.4.0 post adopts the stored values' );

exit( $georg_failures ? 1 : 0 );
